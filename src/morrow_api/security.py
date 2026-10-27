import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Every admin route takes the same audited reason header: one definition, one set of rules.
Reason = Annotated[
    str, Header(alias="X-Admin-Reason", min_length=10, max_length=300, pattern=r"^[^\x00-\x1f]+$")
]

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))
bearer = HTTPBearer(auto_error=False)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(encoded: str | None, password: str) -> bool:
    try:
        valid = hasher.verify(encoded or DUMMY_HASH, password)
        return bool(encoded) and valid
    except (VerificationError, InvalidHashError):
        return False


def check_login_limit(pool, email: str, ip: str, *, ip_limit: int = 100) -> None:
    # Counts persist across API replicas and restarts. Hash identifiers before storing.
    keys = [("email:" + email, 10), ("ip:" + ip, ip_limit)]
    blocked = False
    with pool.connection() as conn, conn.transaction():
        for key, limit in keys:
            row = conn.execute(
                """INSERT INTO auth_limits(key, attempts) VALUES (%s, 1)
                ON CONFLICT (key) DO UPDATE SET
                  attempts=CASE WHEN auth_limits.window_start < now()-interval '15 minutes' THEN 1 ELSE auth_limits.attempts+1 END,
                  window_start=CASE WHEN auth_limits.window_start < now()-interval '15 minutes' THEN now() ELSE auth_limits.window_start END
                RETURNING attempts""",
                (token_hash(key),),
            ).fetchone()
            blocked |= row["attempts"] > limit
        conn.execute("DELETE FROM auth_limits WHERE window_start < now()-interval '1 day'")
    if blocked:
        raise HTTPException(
            429, "Too many login attempts. Try again later.", headers={"Retry-After": "900"}
        )


def login(pool, settings, email: str, password: str, ip: str):
    if settings.auth_provider != "local":
        raise HTTPException(409, "Use Firebase sign-in for this workspace.")
    check_login_limit(pool, email, ip, ip_limit=settings.auth_ip_attempt_limit)
    with pool.connection() as conn, conn.transaction():
        user = conn.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone()
        valid = verify_password(user["password_hash"] if user else None, password)
        if not valid or not user["active"] or user["firebase_uid"]:
            raise HTTPException(
                401, "Invalid email or password", headers={"WWW-Authenticate": "Bearer"}
            )
        if not user["email_verified"]:
            raise HTTPException(403, "Verify your email before signing in")
        return create_session(conn, settings, user["id"])


def create_session(conn, settings, user_id, firebase_auth_time=None):
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)
    conn.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
    conn.execute(
        "INSERT INTO auth_sessions(token_hash,user_id,expires_at,csrf_hash,firebase_auth_time) VALUES (%s,%s,%s,%s,%s)",
        (token_hash(token), user_id, expires, token_hash(csrf), firebase_auth_time),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "csrf_token": csrf,
        "expires_at": expires,
    }


def attach_session(response, session, settings):
    common = dict(
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_hours * 3600,
    )
    response.set_cookie("morrow_session", session["access_token"], httponly=True, **common)
    response.set_cookie("morrow_csrf", session["csrf_token"], httponly=False, **common)


def current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    token = (
        credentials.credentials
        if credentials and credentials.scheme.lower() == "bearer"
        else request.cookies.get("morrow_session", "")
    )
    if not token or len(token) > 256:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    digest = token_hash(token)
    with request.app.state.pool.connection() as conn:
        user = conn.execute(
            """SELECT u.id,u.tenant_id,u.email,u.name,u.role,u.created_at,u.plan_slug,u.plan_source,u.plan_expires_at,u.subscription_status,u.ai_provider,u.firebase_uid,u.firebase_project_id,s.firebase_auth_time,s.csrf_hash
            FROM auth_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=%s AND s.expires_at > now() AND u.active AND u.email_verified""",
            (digest,),
        ).fetchone()
    if not user:
        raise HTTPException(
            401, "Session expired or invalid", headers={"WWW-Authenticate": "Bearer"}
        )
    csrf_hash = user.pop("csrf_hash")
    if not credentials and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not secrets.compare_digest(
            csrf_hash, token_hash(request.headers.get("x-csrf-token", ""))
        ):
            raise HTTPException(403, "Invalid CSRF token")
    firebase_uid = user.pop("firebase_uid")
    firebase_project_id = user.pop("firebase_project_id")
    firebase_auth_time = user.pop("firebase_auth_time")
    if request.app.state.settings.auth_provider == "firebase":
        # Logout only revokes the presented local session; it remains available
        # during a provider outage and never grants access to user data.
        if request.url.path != "/api/v1/auth/logout":
            try:
                request.app.state.firebase.check_session(
                    firebase_uid, firebase_auth_time, user["email"], firebase_project_id
                )
            except HTTPException as exc:
                if exc.status_code == 401:
                    with request.app.state.pool.connection() as conn:
                        conn.execute("DELETE FROM auth_sessions WHERE token_hash=%s", (digest,))
                raise
    elif firebase_uid or firebase_auth_time:
        # Changing providers never reactivates sessions from the other provider.
        raise HTTPException(401, "Sign in with the configured identity provider.")
    from .entitlements import access_for

    with request.app.state.pool.connection() as conn:
        user.update(access_for(conn, user))
    request.state.token_hash = digest
    return user


def administrator(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user
