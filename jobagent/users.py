"""Accounts: administrator approval workflow, signed single-use review links and account emails.

* The first account is the administrator (role 'admin', status 'approved').
* Sign-ups are 'pending' until the administrator approves them (Admin page or a signed email link).
* Review links are itsdangerous-signed tokens carrying a random nonce that is stored in `signup_tokens`; a link
  works once, for 7 days, and only while the account is still pending. Opening a link only shows a confirmation
  page; the action happens on POST (mail scanners pre-fetch GET links).
* The administrator manages accounts but sees only account info and counts, never another user's private data."""
from __future__ import annotations

import secrets
from datetime import timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .db import Database, audit, iso, parse_iso, utcnow
from .mailer import Mailer, Message

TOKEN_SALT = "jobagent-signup-review"
TOKEN_MAX_AGE = timedelta(days=7)
ACTIONS = ("approve", "reject")

# tables whose rows carry user_id (NULL rows are "orphans", e.g. created by a CLI run before the first account)
OWNED_TABLES = ("applications", "cover_letters", "resume_versions", "uploads", "job_scores", "ai_usage")


class AccountError(Exception):
    pass


# ---------------------------------------------------------------- lookups

def get_user(db: Database, user_id: int) -> dict | None:
    return db.one("SELECT id, username, email, full_name, role, status, created_at, last_login_at, approved_at, "
                  "approved_by, created_ip FROM users WHERE id=?", (user_id,))


def by_username(db: Database, username: str) -> dict | None:
    return db.one("SELECT id, username, email, full_name, role, status FROM users WHERE username=?",
                  ((username or "").strip().lower(),))


def admin_id(db: Database) -> int | None:
    v = db.scalar("SELECT MIN(id) FROM users WHERE role='admin'")
    return int(v) if v is not None else None


def is_admin(db: Database, user_id: int | None) -> bool:
    return bool(user_id) and db.scalar("SELECT role FROM users WHERE id=?", (user_id,)) == "admin"


def list_users(db: Database) -> list[dict]:
    """Account info and COUNTS only (no private content)."""
    rows = db.all(
        "SELECT u.id, u.username, u.email, u.full_name, u.role, u.status, u.created_at, u.last_login_at, "
        "u.approved_at, "
        "(SELECT COUNT(*) FROM applications a WHERE a.user_id=u.id AND a.is_sample=0) AS applications, "
        "(SELECT COUNT(*) FROM resume_versions r WHERE r.user_id=u.id) AS resumes "
        "FROM users u ORDER BY CASE u.status WHEN 'pending' THEN 0 ELSE 1 END, u.id")
    return rows


def adopt_orphans(db: Database, uid: int) -> None:
    """Give rows without an owner (created before any account existed, e.g. by the CLI) to the first admin."""
    with db.transaction():
        for t in OWNED_TABLES:
            db.run(f"UPDATE {t} SET user_id=? WHERE user_id IS NULL", (uid,))
        db.run("UPDATE files SET user_id=? WHERE user_id IS NULL AND kind <> 'uk_sponsor_register'", (uid,))
        from .settings import USER_KEYS
        keys = sorted(USER_KEYS)
        marks = ",".join("?" * len(keys))
        for r in db.all(f"SELECT key, value_json, updated_at FROM settings WHERE key IN ({marks})", keys):
            db.run("INSERT INTO user_settings(user_id, key, value_json, updated_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(user_id, key) DO NOTHING", (uid, r["key"], r["value_json"], r["updated_at"]))
        db.run(f"DELETE FROM settings WHERE key IN ({marks})", keys)
        for r in db.all("SELECT id FROM jobs WHERE bookmarked=1"):
            db.run("INSERT INTO user_bookmarks(user_id, job_id, created_at) VALUES (?,?,?) "
                   "ON CONFLICT(user_id, job_id) DO NOTHING", (uid, r["id"], iso()))
        db.run("UPDATE jobs SET bookmarked=0 WHERE bookmarked=1")


# ---------------------------------------------------------------- status changes

ALLOWED = {
    "approve": ({"pending", "rejected"}, "approved"),
    "reject": ({"pending"}, "rejected"),
    "suspend": ({"approved"}, "suspended"),
    "reactivate": ({"suspended", "rejected"}, "approved"),
}


def change_status(db: Database, user_id: int, action: str, actor: str, actor_id: int | None = None) -> dict:
    """approve | reject | suspend | reactivate. The administrator account cannot be changed. Every change
    revokes the user's sessions (unless approving) and any outstanding review links."""
    if action not in ALLOWED:
        raise AccountError("Unknown action.")
    frm_ok, to = ALLOWED[action]
    with db.transaction():
        u = get_user(db, user_id)
        if not u:
            raise AccountError("User not found.")
        if u["role"] == "admin":
            raise AccountError("The administrator account cannot be changed here.")
        if u["status"] not in frm_ok:
            raise AccountError(f"Cannot {action} an account that is {u['status']}.")
        if to == "approved":
            db.run("UPDATE users SET status='approved', approved_at=?, approved_by=? WHERE id=?",
                   (iso(), actor_id, user_id))
        else:
            db.run("UPDATE users SET status=? WHERE id=?", (to, user_id))
            db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.run("UPDATE signup_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (iso(), user_id))
        audit(db, actor=actor, entity="admin", entity_id=user_id, action=f"user_{action}",
              from_status=u["status"], to_status=to, detail={"username": u["username"]})
    return get_user(db, user_id)


def delete_user(db: Database, user_id: int, actor: str) -> None:
    """Permanently delete an account and ALL of its private data (cascade). The admin cannot be deleted."""
    with db.transaction():
        u = get_user(db, user_id)
        if not u:
            raise AccountError("User not found.")
        if u["role"] == "admin":
            raise AccountError("The administrator account cannot be deleted.")
        db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
        # approvals/uploads/cover letters reference applications/resumes/files; delete children explicitly so the
        # order is deterministic on both dialects
        db.run("DELETE FROM approvals WHERE application_id IN (SELECT id FROM applications WHERE user_id=?)", (user_id,))
        db.run("DELETE FROM uploads WHERE user_id=? OR application_id IN (SELECT id FROM applications WHERE user_id=?)",
               (user_id, user_id))
        db.run("DELETE FROM applications WHERE user_id=?", (user_id,))
        db.run("DELETE FROM cover_letters WHERE user_id=?", (user_id,))
        db.run("UPDATE resume_versions SET parent_id=NULL WHERE user_id=?", (user_id,))
        db.run("DELETE FROM resume_versions WHERE user_id=?", (user_id,))
        db.run("DELETE FROM files WHERE user_id=?", (user_id,))
        db.run("DELETE FROM users WHERE id=?", (user_id,))
        audit(db, actor=actor, entity="admin", entity_id=user_id, action="user_delete",
              detail={"username": u["username"]})


# ---------------------------------------------------------------- signed review links

def _serializer(secret: str) -> URLSafeTimedSerializer:
    if not secret or len(secret) < 32:
        raise AccountError("JOBAGENT_SECRET_KEY is not configured")
    return URLSafeTimedSerializer(secret, salt=TOKEN_SALT)


def create_review_tokens(db: Database, secret: str, user_id: int) -> dict[str, str]:
    """One signed token per action, each bound to a fresh random nonce stored in the database."""
    out = {}
    now = utcnow()
    with db.transaction():
        for action in ACTIONS:
            nonce = secrets.token_urlsafe(24)
            db.run("INSERT INTO signup_tokens(nonce, user_id, action, created_at, expires_at) VALUES (?,?,?,?,?)",
                   (nonce, user_id, action, iso(now), iso(now + TOKEN_MAX_AGE)))
            out[action] = _serializer(secret).dumps({"n": nonce, "u": user_id, "a": action})
    return out


def check_review_token(db: Database, secret: str, token: str) -> dict:
    """Validate a review token WITHOUT consuming it. Returns {'action', 'user', 'nonce'}; raises AccountError."""
    if not token or len(token) > 500:
        raise AccountError("This link is not valid.")
    try:
        data = _serializer(secret).loads(token, max_age=int(TOKEN_MAX_AGE.total_seconds()))
    except SignatureExpired:
        raise AccountError("This link has expired. Use the Admin page instead.") from None
    except BadSignature:
        raise AccountError("This link is not valid.") from None
    if not isinstance(data, dict) or data.get("a") not in ACTIONS:
        raise AccountError("This link is not valid.")
    row = db.one("SELECT * FROM signup_tokens WHERE nonce=?", (str(data.get("n")),))
    if not row or int(row["user_id"]) != int(data.get("u") or 0) or row["action"] != data["a"]:
        raise AccountError("This link is not valid.")
    if row["used_at"]:
        raise AccountError("This link has already been used.")
    if (parse_iso(row["expires_at"]) or utcnow()) <= utcnow():
        raise AccountError("This link has expired. Use the Admin page instead.")
    user = get_user(db, row["user_id"])
    if not user:
        raise AccountError("This link is not valid.")
    if user["status"] != "pending":
        raise AccountError(f"This sign-up was already handled (the account is {user['status']}).")
    return {"action": row["action"], "user": user, "nonce": row["nonce"]}


def use_review_token(db: Database, secret: str, token: str) -> dict:
    """Validate and CONSUME a review token (single use; sibling links for the same account are invalidated),
    then apply the action. Returns the updated user."""
    info = check_review_token(db, secret, token)
    with db.transaction():
        n = db.run("UPDATE signup_tokens SET used_at=? WHERE nonce=? AND used_at IS NULL", (iso(), info["nonce"]))
        if n != 1:
            raise AccountError("This link has already been used.")
        user = change_status(db, info["user"]["id"], info["action"], actor="admin:email-link",
                             actor_id=admin_id(db))
    return {"action": info["action"], "user": user}


# ---------------------------------------------------------------- emails

def _who(u: dict) -> str:
    return u.get("full_name") or u.get("username") or "there"


def send_signup_request(db: Database, mailer: Mailer, *, admin_email: str | None, base_url: str | None,
                        user: dict, tokens: dict[str, str] | None) -> dict | None:
    """Email the administrator about a new sign-up with Approve / Reject links."""
    if not admin_email:
        to = db.scalar("SELECT email FROM users WHERE role='admin' AND email IS NOT NULL ORDER BY id LIMIT 1")
        if not to:
            # still recorded in the outbox so the pending request is visible on the Admin page
            to = "admin@localhost"
    else:
        to = admin_email
    links, redact = [], []
    if base_url and tokens:
        for action in ACTIONS:
            url = f"{base_url}/signup/review/{tokens[action]}"
            links.append(f"{action.capitalize()}: {url}")
            redact.append(url)
    text = (f"A new JobAgent account is waiting for your approval.\n\n"
            f"Name:     {user.get('full_name') or '-'}\n"
            f"Email:    {user.get('email') or '-'}\n"
            f"Username: {user.get('username')}\n"
            f"Signed up: {user.get('created_at')} (UTC)\n\n")
    if links:
        text += ("Review this request (each link opens a confirmation page, works once and expires in 7 days):\n"
                 + "\n".join(links) + "\n\n")
    text += ("You can also approve or reject it on the Admin page"
             + (f": {base_url}/admin/users" if base_url else ".") + "\n")
    return mailer.send(db, Message(to=to, subject=f"JobAgent sign-up request: {user.get('username')}", text=text,
                                   kind="signup_request", related_user_id=user.get("id"), redact=redact))


def send_decision(db: Database, mailer: Mailer, *, base_url: str | None, user: dict, action: str) -> dict | None:
    """Tell the applicant the outcome (approved -> can sign in; rejected -> polite note)."""
    if not user.get("email"):
        return None
    if action in ("approve", "reactivate"):
        subject = "Your JobAgent account is approved"
        text = (f"Hello {_who(user)},\n\nYour JobAgent account has been approved. You can now sign in"
                + (f" at {base_url}/login" if base_url else "") + f" with the username '{user['username']}'.\n")
    elif action == "reject":
        subject = "Your JobAgent sign-up request"
        text = (f"Hello {_who(user)},\n\nThank you for your interest in JobAgent. Unfortunately your sign-up request "
                "was not approved. No further action is needed.\n")
    else:
        return None
    return mailer.send(db, Message(to=user["email"], subject=subject, text=text, kind=f"applicant_{action}",
                                   related_user_id=user.get("id")))
