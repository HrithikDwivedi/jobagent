"""Outgoing email behind a small provider interface.

Providers: Resend HTTP API (RESEND_API_KEY), SMTP (SMTP_HOST/PORT/USER/PASSWORD/FROM, e.g. a Gmail app password) and a
console/log provider used when nothing is configured. Every message is recorded in the `outbox` table (subject,
recipient, status, provider, error) so the administrator can see what was (or could not be) sent. Secrets are never
logged or stored; sending never raises (email failures must not break sign-up)."""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from .db import Database, iso

log = logging.getLogger("jobagent.mail")
DEFAULT_FROM = "JobAgent <onboarding@resend.dev>"


class MailError(Exception):
    pass


@dataclass
class Message:
    to: str
    subject: str
    text: str
    kind: str = "general"
    related_user_id: int | None = None
    # substrings (e.g. signed links) replaced by "[link removed]" in the stored outbox copy
    redact: list[str] = field(default_factory=list)


class Provider:
    name = "console"

    def send(self, msg: Message, sender: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleProvider(Provider):
    """Nothing is configured: the message is only recorded in the outbox (status 'logged')."""
    name = "console"

    def send(self, msg: Message, sender: str) -> None:
        log.info("email (not sent, no provider configured): to=%s subject=%s", msg.to, msg.subject)


class ResendProvider(Provider):
    name = "resend"
    URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, timeout: float = 15.0):
        self._key = api_key
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def send(self, msg: Message, sender: str) -> None:
        try:
            r = self._client.post(self.URL, json={"from": sender, "to": [msg.to], "subject": msg.subject,
                                                  "text": msg.text},
                                  headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as e:
            raise MailError(f"Resend unreachable ({type(e).__name__})") from None
        if r.status_code in (401, 403):
            raise MailError(f"Resend rejected the request (HTTP {r.status_code}); check RESEND_API_KEY and the "
                            "sender domain")
        if r.status_code >= 300:
            detail = ""
            try:
                detail = str((r.json() or {}).get("message", ""))[:160]
            except ValueError:
                pass
            raise MailError(f"Resend error HTTP {r.status_code}" + (f": {detail}" if detail else ""))


class SMTPProvider(Provider):
    name = "smtp"

    def __init__(self, host: str, port: int, user: str | None, password: str | None, timeout: float = 20.0):
        self.host, self.port, self.user, self._password, self.timeout = host, port, user, password, timeout

    def send(self, msg: Message, sender: str) -> None:
        em = EmailMessage()
        em["From"], em["To"], em["Subject"] = sender, msg.to, msg.subject
        em.set_content(msg.text)
        ctx = ssl.create_default_context()
        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx) as s:
                    if self.user:
                        s.login(self.user, self._password or "")
                    s.send_message(em)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                    s.starttls(context=ctx)
                    if self.user:
                        s.login(self.user, self._password or "")
                    s.send_message(em)
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"SMTP error ({type(e).__name__})") from None


class Mailer:
    def __init__(self, provider: Provider, sender: str = DEFAULT_FROM):
        self.provider = provider
        self.sender = sender

    def send(self, db: Database, msg: Message) -> dict:
        """Send one message and record it in the outbox. Never raises; returns the outbox row summary."""
        stored = msg.text
        for s in msg.redact:
            if s:
                stored = stored.replace(s, "[link removed]")
        try:
            with db.transaction():
                oid = db.insert(
                    "INSERT INTO outbox(kind, to_addr, subject, body_text, status, provider, related_user_id, created_at)"
                    " VALUES (?,?,?,?, 'queued', ?, ?, ?)",
                    (msg.kind[:40], msg.to[:320], msg.subject[:300], stored[:20000], self.provider.name,
                     msg.related_user_id, iso()))
        except Exception:  # the outbox itself is best-effort
            log.exception("could not record outbox row")
            oid = None
        status, error = "sent", None
        try:
            self.provider.send(msg, self.sender)
            if isinstance(self.provider, ConsoleProvider):
                status = "logged"
        except MailError as e:
            status, error = "failed", str(e)[:300]
        except Exception as e:  # never let email break the caller
            status, error = "failed", f"{type(e).__name__}"
        if error:
            log.warning("email to %s failed via %s: %s", msg.to, self.provider.name, error)
        if oid is not None:
            try:
                with db.transaction():
                    db.run("UPDATE outbox SET status=?, error=?, sent_at=? WHERE id=?",
                           (status, error, iso() if status == "sent" else None, oid))
            except Exception:
                log.exception("could not update outbox row")
        return {"id": oid, "status": status, "error": error, "provider": self.provider.name}


def make_mailer(cfg) -> Mailer:
    """Resend if RESEND_API_KEY is set, else SMTP if SMTP_HOST is set, else the console/log provider."""
    if getattr(cfg, "resend_api_key", None):
        return Mailer(ResendProvider(cfg.resend_api_key), cfg.mail_from or DEFAULT_FROM)
    if getattr(cfg, "smtp_host", None):
        sender = cfg.smtp_from or cfg.mail_from or cfg.smtp_user or DEFAULT_FROM
        return Mailer(SMTPProvider(cfg.smtp_host, int(cfg.smtp_port or 587), cfg.smtp_user, cfg.smtp_password), sender)
    return Mailer(ConsoleProvider(), getattr(cfg, "mail_from", None) or DEFAULT_FROM)


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}$")


def valid_email(addr: str | None) -> bool:
    return bool(addr and len(addr) <= 254 and EMAIL_RE.match(addr))
