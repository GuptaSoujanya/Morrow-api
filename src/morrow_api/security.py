import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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


def check_login_limit(pool, email: str, ip: str) -> None:
    # Counts persist across API replicas and restarts. Hash identifiers before storing.
    keys = [("email:" + email, 10), ("ip:" + ip, 100)]
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
        raise HTTPException(429, "Too many login attempts. Try again later.", headers={"Retry-After": "900"})


def login(pool, settings, email: str, password: str, ip: str):
    check_login_limit(pool, email, ip)
    with pool.connection() as conn, conn.transaction():
        user = conn.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone()
        valid = verify_password(user["password_hash"] if user else None, password)
        if not valid or not user["active"]:
            raise HTTPException(401, "Invalid email or password", headers={"WWW-Authenticate": "Bearer"})
        token = secrets.token_urlsafe(48)
        expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)
        conn.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
        conn.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)", (token_hash(token), user["id"], expires))
    return {"access_token": token, "token_type": "bearer", "expires_at": expires}


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != "bearer" or len(credentials.credentials) > 256:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    digest = token_hash(credentials.credentials)
    with request.app.state.pool.connection() as conn:
        user = conn.execute(
            """SELECT u.id,u.tenant_id,u.email,u.role,u.created_at
            FROM auth_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=%s AND s.expires_at > now() AND u.active""", (digest,)
        ).fetchone()
    if not user:
        raise HTTPException(401, "Session expired or invalid", headers={"WWW-Authenticate": "Bearer"})
    request.state.token_hash = digest
    return user


def administrator(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user
