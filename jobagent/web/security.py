"""Authentication, sessions, CSRF and login rate limiting. Server-side only; nothing trusts the browser."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..db import Database, audit, iso, parse_iso, utcnow

SESSION_COOKIE = "ja_session"
_ph = PasswordHasher()  # argon2id with library defaults
DUMMY_HASH = _ph.hash("not-a-real-password-" + secrets.token_hex(8))

MAX_FAILS_PER_USER = 5
MAX_FAILS_PER_IP = 20
LOCK_WINDOW = timedelta(minutes=15)


class AuthError(Exception):
    pass


def password_problems(pw: str, username: str = "") -> list[str]:
    problems = []
    if len(pw) < 12:
        problems.append("at least 12 characters")
    if pw.lower() == pw or pw.upper() == pw:
        problems.append("upper- and lower-case letters")
    if not re.search(r"\d", pw):
        problems.append("a digit")
    if username and username.lower() in pw.lower():
        problems.append("must not contain the username")
    if pw.lower() in ("password1234", "changeme1234", "jobagent1234", "qwertyuiop12"):
        problems.append("not a common password")
    return problems


def user_count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users") or 0)


USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[a-z0-9.-]{1,255}\.[a-z]{2,}$")


class UsernameTaken(AuthError):
    pass


class EmailTaken(AuthError):
    pass


def create_user(db: Database, username: str, password: str, actor: str = "setup", *, email: str | None = None,
                full_name: str | None = None, role: str | None = None, status: str | None = None,
                created_ip: str | None = None) -> int:
    """Create an account. The FIRST account is always the approved administrator; later accounts default to
    role 'user', status 'pending' (they cannot sign in until the administrator approves them)."""
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 3-40 characters: letters, digits, dot, dash or underscore.")
    problems = password_problems(password, username)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    email = (email or "").strip().lower() or None
    if email and (len(email) > 254 or not EMAIL_RE.match(email)):
        raise AuthError("Enter a valid email address.")
    full_name = " ".join((full_name or "").split())[:120] or None
    now = iso()
    with db.transaction():
        if db.one("SELECT id FROM users WHERE username=?", (username,)):
            raise UsernameTaken("That username is not available; choose another.")
        if email and db.one("SELECT id FROM users WHERE email=?", (email,)):
            raise EmailTaken("email in use")
        first = user_count(db) == 0
        role = "admin" if first else (role or "user")
        status = "approved" if first else (status or "pending")
        uid = db.insert("INSERT INTO users(username, password_hash, created_at, password_changed_at, email, full_name, "
                        "role, status, approved_at, created_ip) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (username, _ph.hash(password), now, now, email, full_name, role, status,
                         now if status == "approved" else None, (created_ip or "")[:64] or None))
        audit(db, actor=actor, entity="auth", entity_id=uid, action="create_user",
              detail={"username": username, "role": role, "status": status})
        if first:
            from ..users import adopt_orphans
            adopt_orphans(db, uid)
    return uid


def set_password(db: Database, username: str, password: str, actor: str) -> None:
    username = username.strip().lower()
    problems = password_problems(password, username)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    with db.transaction():
        u = db.one("SELECT id FROM users WHERE username=?", (username,))
        if not u:
            raise AuthError("No such user.")
        db.run("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?", (_ph.hash(password), iso(), u["id"]))
        db.run("DELETE FROM sessions WHERE user_id=?", (u["id"],))  # log out everywhere
        audit(db, actor=actor, entity="auth", entity_id=u["id"], action="password_reset")


def _recent_failures(db: Database, *, username: str | None = None, ip: str | None = None) -> int:
    since = iso(utcnow() - LOCK_WINDOW)
    if username is not None:
        return int(db.scalar("SELECT COUNT(*) FROM login_attempts WHERE username=? AND success=0 AND at >= ?",
                             (username, since)) or 0)
    return int(db.scalar("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND success=0 AND at >= ?", (ip, since)) or 0)


def authenticate(db: Database, username: str, password: str, ip: str) -> dict:
    """Returns the user row or raises AuthError with a deliberately generic message."""
    username = (username or "").strip().lower()[:60]
    if _recent_failures(db, username=username) >= MAX_FAILS_PER_USER or _recent_failures(db, ip=ip) >= MAX_FAILS_PER_IP:
        with db.transaction():
            db.run("INSERT INTO login_attempts(username, ip, success, at) VALUES (?,?,0,?)", (username, ip, iso()))
        raise AuthError("Too many failed attempts. Try again in 15 minutes.")
    u = db.one("SELECT * FROM users WHERE username=?", (username,))
    ok = False
    try:
        ok = _ph.verify(u["password_hash"] if u else DUMMY_HASH, password or "") and u is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    with db.transaction():
        db.run("INSERT INTO login_attempts(username, ip, success, at) VALUES (?,?,?,?)", (username, ip, int(ok), iso()))
        if ok:
            db.run("UPDATE users SET last_login_at=? WHERE id=?", (iso(), u["id"]))
            if _ph.check_needs_rehash(u["password_hash"]):
                db.run("UPDATE users SET password_hash=? WHERE id=?", (_ph.hash(password), u["id"]))
    if not ok:
        audit(db, actor="anonymous", entity="auth", entity_id=None, action="login_failed",
              detail={"username": username[:40]})
        raise AuthError("Invalid username or password.")
    # Account state is only revealed AFTER the correct password, so it never discloses whether a username exists.
    status = u.get("status") or "approved"
    if status != "approved":
        with db.transaction():
            db.run("UPDATE users SET last_login_at=? WHERE id=?", (u["last_login_at"], u["id"]))
        audit(db, actor=f"user:{username}", entity="auth", entity_id=u["id"], action="login_refused",
              detail={"status": status})
        raise AccountNotActive(status, STATUS_MESSAGES.get(status, "This account cannot sign in."))
    audit(db, actor=f"user:{username}", entity="auth", entity_id=u["id"], action="login")
    return u


STATUS_MESSAGES = {
    "pending": "Your account is awaiting approval by the administrator. You will receive an email when it is "
               "approved.",
    "rejected": "Your sign-up request was not approved.",
    "suspended": "This account is suspended. Contact the administrator.",
}


class AccountNotActive(AuthError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


SIGNUPS_PER_IP_PER_HOUR = 5


def signup_rate_limited(db: Database, ip: str) -> bool:
    since = iso(utcnow() - timedelta(hours=1))
    n = int(db.scalar("SELECT COUNT(*) FROM login_attempts WHERE username='#signup' AND ip=? AND at >= ?",
                      (ip, since)) or 0)
    return n >= SIGNUPS_PER_IP_PER_HOUR


def record_signup_attempt(db: Database, ip: str) -> None:
    with db.transaction():
        db.run("INSERT INTO login_attempts(username, ip, success, at) VALUES ('#signup', ?, 1, ?)", (ip, iso()))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Database, user_id: int, absolute_hours: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = utcnow()
    with db.transaction():
        db.run("INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, last_seen_at, absolute_expires_at) "
               "VALUES (?,?,?,?,?,?)", (_hash_token(token), user_id, csrf, iso(now), iso(now),
                                         iso(now + timedelta(hours=absolute_hours))))
        db.run("DELETE FROM sessions WHERE absolute_expires_at < ?", (iso(now),))
    return token, csrf


def load_session(db: Database, token: str | None, idle_minutes: int) -> dict | None:
    if not token or len(token) > 200:
        return None
    s = db.one("SELECT s.*, u.username, u.role, u.status AS user_status, u.full_name, u.email FROM sessions s "
               "JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (_hash_token(token),))
    if not s:
        return None
    if (s.get("user_status") or "approved") != "approved":
        # suspended / no longer approved: the session is revoked immediately
        with db.transaction():
            db.run("DELETE FROM sessions WHERE user_id=?", (s["user_id"],))
        return None
    now = utcnow()
    last = parse_iso(s["last_seen_at"]) or now
    absolute = parse_iso(s["absolute_expires_at"]) or now
    if now > absolute or now - last > timedelta(minutes=idle_minutes):
        with db.transaction():
            db.run("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
        return None
    if (now - last).total_seconds() > 60:  # avoid a write on every request
        with db.transaction():
            db.run("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (iso(now), s["token_hash"]))
    return s


def destroy_session(db: Database, token: str | None) -> None:
    if token:
        with db.transaction():
            db.run("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def csrf_ok(session: dict | None, supplied: str | None) -> bool:
    return bool(session and supplied and hmac.compare_digest(session["csrf_token"], supplied))


def setup_token_ok(expected: str | None, supplied: str | None) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))
