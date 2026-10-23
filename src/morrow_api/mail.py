"""Private transactional outbox with file, SMTP, and optional Resend delivery."""

import html
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_ERROR_CODES = {
    "daily_quota_exceeded",
    "monthly_quota_exceeded",
    "rate_limit_exceeded",
    "concurrent_idempotent_requests",
    "invalid_idempotent_request",
    "validation_error",
    "missing_api_key",
    "invalid_api_key",
    "restricted_api_key",
    "missing_required_field",
    "application_error",
    "service_unavailable",
}


class DeliveryError(Exception):
    """Only a safe provider code is persisted; never credentials or message bodies."""

    def __init__(self, code, *, retry_at=None, pause_queue=False):
        self.code, self.retry_at, self.pause_queue = code, retry_at, pause_queue
        super().__init__(code)


def queue_mail(conn, email: str, subject: str, body: str, *, expires_at=None):
    conn.execute(
        "INSERT INTO mail_outbox(id,recipient,subject,body,expires_at) VALUES (%s,%s,%s,%s,%s)",
        (uuid4(), email, subject, body, expires_at),
    )


def html_body(subject, body, public_origin):
    """Render plain text safely. Only our own verification/reset links become CTAs."""
    sections = body.split("\n\n")
    heading = sections[0].strip() if sections else subject
    auth_message = heading in ("Verify your email", "Reset your password")
    content_sections = sections[1:] if auth_message else sections
    if not auth_message:
        heading = subject.removesuffix(" · Morrow")
    action = None
    origin = urlsplit(public_origin)
    for paragraph in sections[1:]:
        candidate = paragraph.strip()
        try:
            target = urlsplit(candidate)
        except ValueError:
            continue
        if (
            auth_message
            and (target.scheme, target.netloc) == (origin.scheme, origin.netloc)
            and target.path == "/"
            and not target.query
            and re.fullmatch(r"/(verify|reset)\?token=[A-Za-z0-9_-]+", target.fragment)
        ):
            action = candidate
            break
    paragraphs = []
    for paragraph in content_sections:
        if action and paragraph.strip() == action:
            continue
        paragraphs.append(
            '<p style="margin:0 0 20px;line-height:1.7;color:#b7bac7">'
            + html.escape(paragraph).replace("\n", "<br>")
            + "</p>"
        )
    if action:
        link = html.escape(action, quote=True)
        button = (
            '<p style="margin:28px 0"><a href="' + link + '" '
            'style="display:inline-block;background:#b9a5ff;color:#241b3a;'
            "text-decoration:none;font-size:16px;font-weight:700;padding:16px 24px;"
            'border-radius:12px">' + html.escape(heading) + "</a></p>"
            '<p style="margin:24px 0 8px;color:#b7bac7;font-size:13px">'
            "If the button does not open, copy this link into your browser:</p>"
            '<p style="margin:0 0 24px;word-break:break-all;font-size:13px;line-height:1.6">'
            '<a href="' + link + '" style="color:#b9a5ff">' + link + "</a></p>"
        )
    else:
        button = ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark light"><title>'
        + html.escape(subject)
        + '</title></head><body style="margin:0;padding:32px 16px;background:#121318;'
        'font-family:Arial,Helvetica,sans-serif;color:#f2f3f7">'
        '<table role="presentation" style="width:100%;max-width:560px;margin:0 auto;'
        'border-spacing:0;background:#1b1d24;border:1px solid #30333d;border-radius:20px">'
        '<tr><td style="padding:32px"><p style="margin:0 0 36px;font-size:26px;'
        'font-weight:700;letter-spacing:-1px;color:#f2f3f7">morrow<span style="color:#b9a5ff">.</span></p>'
        '<p style="margin:0 0 12px;font-size:11px;font-weight:700;letter-spacing:2px;color:#b9a5ff">YOUR PRIVATE WORKSPACE</p>'
        '<h1 style="margin:0 0 20px;font-size:28px;line-height:1.25;color:#f2f3f7">'
        + html.escape(heading)
        + "</h1>"
        + button
        + "".join(paragraphs)
        + '<p style="margin:32px 0 0;padding-top:20px;border-top:1px solid #30333d;'
        'font-size:12px;line-height:1.6;color:#a0a5b6">Morrow · Your next chapter, thoughtfully planned.</p>'
        "</td></tr></table></body></html>"
    )


def build_message(row, settings):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = (
        settings.mail_from,
        row["recipient"],
        row["subject"],
    )
    message["Message-ID"] = f"<{row['id']}@morrow.local>"
    message.set_content(row["body"])
    message.add_alternative(
        html_body(row["subject"], row["body"], settings.public_origin), subtype="html"
    )
    return message


def resend_retry_at(response, code):
    now = datetime.now(timezone.utc)
    if code == "daily_quota_exceeded":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    if code == "monthly_quota_exceeded":
        following_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
        return following_month.replace(hour=0, minute=0, second=5, microsecond=0)
    try:
        delay = max(60, min(86400, int(response.headers.get("Retry-After", "60"))))
    except ValueError:
        delay = 60
    return now + timedelta(seconds=delay)


def deliver_resend(row, settings, message):
    key = settings.resend_api_key.get_secret_value()
    if not key:
        raise DeliveryError("resend_not_configured", pause_queue=True)
    # The fixed endpoint and disabled redirects keep the API key on Resend's TLS origin.
    response = httpx.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": "Bearer " + key,
            "Idempotency-Key": "morrow-mail/" + str(row["id"]),
        },
        json={
            "from": settings.mail_from,
            "to": [row["recipient"]],
            "subject": row["subject"],
            "text": row["body"],
            "html": message.get_body(preferencelist=("html",)).get_content(),
        },
        timeout=15,
        follow_redirects=False,
    )
    try:
        result = response.json()
    except ValueError:
        result = {}
    if not 200 <= response.status_code < 300:
        code = result.get("name", "") if isinstance(result, dict) else ""
        if not isinstance(code, str) or code not in RESEND_ERROR_CODES:
            code = "resend_http_" + str(response.status_code)
        raise DeliveryError(
            code,
            retry_at=resend_retry_at(response, code) if response.status_code == 429 else None,
            pause_queue=response.status_code in (401, 403, 429) or response.status_code >= 500,
        )
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise DeliveryError("resend_unconfirmed_response", pause_queue=True)


def deliver(row, settings):
    message = build_message(row, settings)
    if settings.mail_backend == "file":
        settings.mail_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = settings.mail_root / f"{row['id']}.eml"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(message.as_bytes())
    elif settings.mail_backend == "resend":
        deliver_resend(row, settings, message)
    elif settings.mail_backend == "smtp":
        if not settings.smtp_host:
            raise DeliveryError("smtp_not_configured", pause_queue=True)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            client.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            client.send_message(message)
    else:
        raise DeliveryError("mail_backend_not_configured", pause_queue=True)


def flush_mail(pool, settings, limit=20):
    sent = 0
    with pool.connection() as conn, conn.transaction():
        # Expired recovery links are useless to recipients and should not remain stored.
        conn.execute(
            "UPDATE mail_outbox SET body='',last_error_code='expired' WHERE sent_at IS NULL AND expires_at <= now() AND body <> ''"
        )
    # Row locks avoid concurrent claims. Resend deduplicates the queue UUID for 24h;
    # SMTP still cannot promise exactly once if its acknowledgement is lost.
    for _ in range(limit):
        pause = False
        with pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM mail_outbox WHERE sent_at IS NULL AND attempts < 5 AND next_attempt_at <= now() AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not row:
                break
            conn.execute("UPDATE mail_outbox SET attempts=attempts+1 WHERE id=%s", (row["id"],))
            failure = None
            try:
                deliver(row, settings)
            except DeliveryError as exc:
                failure = exc
            except (OSError, smtplib.SMTPException, httpx.HTTPError):
                failure = DeliveryError("transport_unavailable", pause_queue=True)
            except (ValueError, TypeError):
                failure = DeliveryError("invalid_message")
            if failure:
                retry_at = failure.retry_at or datetime.now(timezone.utc) + timedelta(
                    seconds=min(3600, 60 * 4 ** row["attempts"])
                )
                conn.execute(
                    "UPDATE mail_outbox SET next_attempt_at=%s,last_error_code=%s WHERE id=%s",
                    (retry_at, failure.code, row["id"]),
                )
                pause = failure.pause_queue
                if pause:
                    # A quota/provider outage applies to every currently queued email.
                    conn.execute(
                        "UPDATE mail_outbox SET next_attempt_at=GREATEST(next_attempt_at,%s) WHERE id IN (SELECT id FROM mail_outbox WHERE sent_at IS NULL AND attempts < 5 FOR UPDATE SKIP LOCKED)",
                        (retry_at,),
                    )
            else:
                conn.execute(
                    "UPDATE mail_outbox SET sent_at=now(),body='',last_error_code=NULL WHERE id=%s",
                    (row["id"],),
                )
                sent += 1
        if pause:
            break
    return sent
