"""K. Multi-user accounts: sign-up + admin approval (page and signed email links), email providers, per-user data
isolation (no route or guessed ID ever exposes another user's private data), AI restricted to the admin, per-user
agent scoring, and migration 0004 backfilling single-user data to the admin. All data is SYNTHETIC."""
from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD, TEST_USER, _hidden, extract_csrf, web_factory
from factories import (ADMIN, FakeSource, agent_config, ensure_admin, make_raw, make_user, no_network_transport,
                       pdf_bytes)
from jobagent import agent
from jobagent import applications as apps
from jobagent import settings as settings_mod
from jobagent import users as users_mod
from jobagent.ai import ADMIN_ONLY_REASON, AIService, Completion, Provider
from jobagent.db import Database, dumps, iso, open_db, split_sql, utcnow, MIGRATIONS_DIR
from jobagent.http import HttpClient
from jobagent.mailer import Mailer, MailError, Message, ResendProvider
from jobagent.mailer import Provider as MailProvider
from jobagent.resume import store
from jobagent.web import queries, security
from webdata import insert_job

SECRET = "k" * 40


# ----------------------------------------------------------------------------- helpers

class CaptureProvider(MailProvider):
    """Records messages instead of sending them (looks like a real provider: status 'sent')."""
    name = "capture"

    def __init__(self):
        self.sent: list[Message] = []

    def send(self, msg, sender):
        self.sent.append(msg)


class FailingProvider(MailProvider):
    name = "broken"

    def send(self, msg, sender):
        raise MailError("SMTP error (ConnectionRefusedError)")


def capture_web(tmp_path, provider=None, **cfg):
    prov = provider or CaptureProvider()
    w = web_factory(tmp_path, mailer=Mailer(prov, "JobAgent <test@example.com>"), **cfg)
    w.provider = prov
    w.setup_user()
    return w


def links(msg: Message) -> dict:
    out = {}
    for action, url in re.findall(r"(Approve|Reject): (\S+)", msg.text):
        out[action.lower()] = "/" + url.split("/", 3)[3]
    return out


def login_ok(w, username, password=TEST_PASSWORD) -> None:
    r = w.login(username, password)
    assert r.status_code == 303, r.text[:400]


def fresh_client(w) -> TestClient:
    """Another browser (e.g. the admin's phone) on the same app; no cookies."""
    return TestClient(w.client.app)


def user_status(w, username) -> str | None:
    d = w.db()
    try:
        return d.scalar("SELECT status FROM users WHERE username=?", (username,))
    finally:
        d.close()


# ----------------------------------------------------------------------------- sign-up

def test_signup_creates_pending_user_and_emails_admin(tmp_path):
    """Expected: sign-up creates a PENDING user, records an outbox row and emails JOBAGENT_ADMIN_EMAIL with the
    applicant's details and both Approve and Reject links (links are redacted in the stored outbox copy)."""
    w = capture_web(tmp_path)
    r = w.signup("bea", full_name="Bea Example", email="Bea@Example.com")
    assert r.status_code == 200 and "awaiting approval" in r.text.lower()
    assert user_status(w, "bea") == "pending"
    d = w.db()
    u = d.one("SELECT * FROM users WHERE username='bea'")
    assert u["role"] == "user" and u["email"] == "bea@example.com" and u["full_name"] == "Bea Example"
    msg = w.provider.sent[-1]
    assert msg.to == "owner@example.com" and msg.kind == "signup_request"
    assert "Bea Example" in msg.text and "bea@example.com" in msg.text and "bea" in msg.text
    assert u["created_at"][:16] in msg.text
    lk = links(msg)
    assert set(lk) == {"approve", "reject"} and lk["approve"] != lk["reject"]
    row = d.one("SELECT * FROM outbox ORDER BY id DESC LIMIT 1")
    assert row["status"] == "sent" and row["to_addr"] == "owner@example.com" and row["related_user_id"] == u["id"]
    assert "/signup/review/" not in row["body_text"] and "[link removed]" in row["body_text"]
    assert d.scalar("SELECT COUNT(*) FROM signup_tokens WHERE user_id=?", (u["id"],)) == 2
    d.close()


def test_signup_without_provider_records_console_outbox(tmp_path):
    """Expected: with no email provider configured the request is still recorded (status 'logged') and visible
    to the admin on the Admin page."""
    w = web_factory(tmp_path)
    w.setup_user()
    assert w.signup("bea").status_code == 200
    d = w.db()
    row = d.one("SELECT * FROM outbox ORDER BY id DESC LIMIT 1")
    d.close()
    assert row["provider"] == "console" and row["status"] == "logged"
    login_ok(w, TEST_USER)
    page = w.client.get("/admin/users").text
    assert "bea@example.com" in page and "Pending sign-ups" in page and "logged" in page


def test_signup_validation_duplicates_and_rate_limit(tmp_path):
    """Expected: mismatched passwords / weak password / taken username are refused; a duplicate EMAIL shows the same
    confirmation without creating an account (no enumeration); the 6th sign-up per IP per hour is rate-limited."""
    w = capture_web(tmp_path)
    assert w.signup("bea", password2="Different-Pass-99").status_code == 400
    assert "Password needs" in w.signup("bea", password="short").text
    assert w.signup("bea").status_code == 200
    r = w.signup("alex", email="new@example.com")
    assert r.status_code == 400 and "not available" in r.text
    r = w.signup("bea2", email="BEA@example.com")
    assert r.status_code == 200 and "awaiting approval" in r.text.lower()
    assert user_status(w, "bea2") is None
    r = w.signup("cara")
    assert r.status_code == 429 and "Too many" in r.text
    assert user_status(w, "cara") is None


@pytest.mark.parametrize("status", ["pending", "rejected", "suspended"])
def test_inactive_accounts_cannot_sign_in(tmp_path, status):
    """Expected: pending/rejected/suspended accounts are refused with a clear message (only after the correct
    password); a wrong password gets the generic message; no session is created."""
    w = capture_web(tmp_path)
    d = w.db()
    make_user(d, "bea", status=status)
    d.close()
    r = w.login("bea", TEST_PASSWORD)
    assert r.status_code == 403 and security.STATUS_MESSAGES[status] in r.text.replace("&#39;", "'")
    assert security.SESSION_COOKIE not in w.client.cookies
    r = w.login("bea", "Wrong-Password-123")
    assert r.status_code == 401 and "Invalid username or password" in r.text
    assert w.client.get("/", follow_redirects=False).status_code == 303


def test_admin_page_approve_and_reject(tmp_path):
    """Expected: the admin approves/rejects from the Admin page; the applicant is emailed; approved users can sign
    in; outstanding email links stop working."""
    w = capture_web(tmp_path)
    w.signup("bea")
    w.signup("cara")
    approve_link = links(w.provider.sent[0])["approve"]
    login_ok(w, TEST_USER)
    d = w.db()
    bea, cara = (d.scalar("SELECT id FROM users WHERE username=?", (n,)) for n in ("bea", "cara"))
    d.close()
    page = w.client.get("/admin/users").text
    assert page.index("Pending sign-ups") < page.index("All accounts")
    tok = extract_csrf(page)
    assert w.client.post(f"/admin/users/{bea}/approve", data={"csrf": tok}, follow_redirects=False).status_code == 303
    assert w.client.post(f"/admin/users/{cara}/reject", data={"csrf": tok}, follow_redirects=False).status_code == 303
    assert user_status(w, "bea") == "approved" and user_status(w, "cara") == "rejected"
    sent = {m.to: m for m in w.provider.sent}
    assert "approved" in sent["bea@example.com"].subject.lower() and "/login" in sent["bea@example.com"].text
    assert "not approved" in sent["cara@example.com"].text
    other = fresh_client(w)
    assert other.get(approve_link).status_code == 400   # link invalidated by the page decision
    w.logout()
    login_ok(w, "bea")
    assert w.client.get("/").status_code == 200


def test_email_link_get_confirms_post_approves_single_use(tmp_path):
    """Expected: GET on the emailed Approve link only shows a confirmation page (scanner-safe); POST approves
    without a login; the link is single-use; the applicant is notified."""
    w = capture_web(tmp_path)
    w.signup("bea", full_name="Bea Example")
    link = links(w.provider.sent[-1])["approve"]
    phone = fresh_client(w)
    r = phone.get(link)
    assert r.status_code == 200 and "Approve this sign-up?" in r.text and "Bea Example" in r.text
    assert user_status(w, "bea") == "pending"          # GET changes nothing
    assert phone.get(link).status_code == 200          # pre-fetching twice is harmless
    pre = _hidden(r.text, "pre_csrf")
    assert phone.post(link, data={}).status_code == 403  # POST needs the page's token
    r = phone.post(link, data={"pre_csrf": pre})
    assert r.status_code == 200 and "Account approved" in r.text
    assert user_status(w, "bea") == "approved"
    assert w.provider.sent[-1].to == "bea@example.com" and "approved" in w.provider.sent[-1].subject.lower()
    r2 = phone.get(link)
    assert r2.status_code == 400 and ("already" in r2.text)
    again = phone.post(link, data={"pre_csrf": pre})
    assert again.status_code in (400, 403) and "Account approved" not in again.text
    d = w.db()
    assert d.scalar("SELECT COUNT(*) FROM signup_tokens WHERE used_at IS NULL") == 0
    d.close()


def test_email_reject_link(tmp_path):
    """Expected: the Reject link rejects the request after confirmation and the applicant gets a polite note."""
    w = capture_web(tmp_path)
    w.signup("bea")
    link = links(w.provider.sent[-1])["reject"]
    phone = fresh_client(w)
    r = phone.get(link)
    assert "Reject this sign-up?" in r.text
    r = phone.post(link, data={"pre_csrf": _hidden(r.text, "pre_csrf")})
    assert r.status_code == 200 and "rejected" in r.text.lower()
    assert user_status(w, "bea") == "rejected"
    assert "not approved" in w.provider.sent[-1].text


def test_expired_and_tampered_links_rejected(tmp_path):
    """Expected: an expired link and a tampered link are refused and change nothing."""
    w = capture_web(tmp_path)
    w.signup("bea")
    lk = links(w.provider.sent[-1])
    phone = fresh_client(w)
    tampered = lk["approve"][:-3] + ("AAA" if not lk["approve"].endswith("AAA") else "BBB")
    r = phone.get(tampered)
    assert r.status_code == 400 and "not valid" in r.text
    swapped = lk["approve"].rsplit("/", 1)[0] + "/" + lk["reject"].rsplit("/", 1)[1][::-1]
    assert phone.get(swapped).status_code == 400
    d = w.db()
    d.run("UPDATE signup_tokens SET expires_at=?", (iso(utcnow() - timedelta(minutes=1)),))
    d.close()
    r = phone.get(lk["approve"])
    assert r.status_code == 400 and "expired" in r.text
    assert user_status(w, "bea") == "pending"


def test_token_signature_expiry_and_nonce_binding(db):
    """Expected: tokens older than 7 days fail the signature check; a token whose nonce row is missing fails."""
    uid = make_user(db, "bea", status="pending")
    toks = users_mod.create_review_tokens(db, SECRET, uid)
    import itsdangerous.timed as t
    real = t.time.time
    try:
        t.time.time = lambda: real() + 8 * 86400
        with pytest.raises(users_mod.AccountError, match="expired"):
            users_mod.check_review_token(db, SECRET, toks["approve"])
    finally:
        t.time.time = real
    db.run("DELETE FROM signup_tokens")
    with pytest.raises(users_mod.AccountError, match="not valid"):
        users_mod.check_review_token(db, SECRET, toks["approve"])
    with pytest.raises(users_mod.AccountError, match="not valid"):
        users_mod.check_review_token(db, "z" * 40, toks["reject"])


def test_email_failure_does_not_break_signup(tmp_path):
    """Expected: a failing provider still creates the pending user; the failure is recorded in the outbox (without
    secrets) and shown on the Admin page."""
    w = capture_web(tmp_path, provider=FailingProvider())
    r = w.signup("bea")
    assert r.status_code == 200 and user_status(w, "bea") == "pending"
    d = w.db()
    row = d.one("SELECT * FROM outbox ORDER BY id DESC LIMIT 1")
    d.close()
    assert row["status"] == "failed" and "SMTP error" in row["error"]
    login_ok(w, TEST_USER)
    page = w.client.get("/admin/users").text
    assert "bea@example.com" in page and "failed" in page


def test_resend_provider_request_shape(db):
    """Expected: Resend gets POST https://api.resend.com/emails with Bearer auth and {from, to[], subject, text};
    HTTP errors become a recorded failure that never contains the API key."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.headers.get("authorization") != "Bearer re_test_key":
            return httpx.Response(401, json={"message": "bad key"})
        return httpx.Response(200, json={"id": "email_123"})

    prov = ResendProvider("re_test_key", transport=httpx.MockTransport(handler))
    res = Mailer(prov, "JobAgent <onboarding@resend.dev>").send(db, Message(to="x@example.com", subject="Hi",
                                                                             text="Body"))
    assert res["status"] == "sent" and res["provider"] == "resend"
    req = seen[-1]
    assert req.method == "POST" and str(req.url) == "https://api.resend.com/emails"
    assert json.loads(req.content) == {"from": "JobAgent <onboarding@resend.dev>", "to": ["x@example.com"],
                                       "subject": "Hi", "text": "Body"}
    bad = Mailer(ResendProvider("re_wrong_key", transport=httpx.MockTransport(handler)))
    res = bad.send(db, Message(to="x@example.com", subject="Hi", text="Body"))
    assert res["status"] == "failed" and "401" in res["error"] and "re_wrong_key" not in res["error"]
    assert "re_wrong_key" not in dumps(db.all("SELECT * FROM outbox"))


def test_make_mailer_selection(tmp_path):
    """Expected: Resend when RESEND_API_KEY is set, SMTP when SMTP_HOST is set, console otherwise."""
    from conftest import make_cfg
    from jobagent.mailer import make_mailer
    assert make_mailer(make_cfg(tmp_path)).provider.name == "console"
    assert make_mailer(make_cfg(tmp_path, smtp_host="smtp.gmail.com", smtp_user="u")).provider.name == "smtp"
    m = make_mailer(make_cfg(tmp_path, resend_api_key="re_x", mail_from="JobAgent <a@b.co>"))
    assert m.provider.name == "resend" and m.sender == "JobAgent <a@b.co>"


def test_admin_test_email_button(tmp_path):
    w = capture_web(tmp_path)
    login_ok(w, TEST_USER)
    tok = extract_csrf(w.client.get("/admin/users").text)
    r = w.client.post("/admin/test-email", data={"csrf": tok, "to": "me@example.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert w.provider.sent[-1].to == "me@example.com" and w.provider.sent[-1].kind == "test"


def test_public_url_must_be_https_except_localhost():
    from jobagent.config import acceptable_base_url
    assert acceptable_base_url("https://jobs.example.com/") == "https://jobs.example.com"
    assert acceptable_base_url("http://localhost:8765") == "http://localhost:8765"
    assert acceptable_base_url("http://jobs.example.com") is None
    assert acceptable_base_url("https://user:pw@jobs.example.com") is None


# ----------------------------------------------------------------------------- admin-only routes

def test_non_admin_gets_404_on_admin_routes(tmp_path):
    w = capture_web(tmp_path)
    d = w.db()
    make_user(d, "bea")
    d.close()
    login_ok(w, "bea")
    tok = w.csrf("/settings")
    assert w.client.get("/admin/users").status_code == 404
    for path in ("/admin/users/1/suspend", "/admin/users/1/delete", "/admin/test-email", "/admin/users/1/approve"):
        assert w.client.post(path, data={"csrf": tok, "confirm_username": "alex"}).status_code == 404
    assert user_status(w, "alex") == "approved"
    # global settings sections are admin-only too
    assert w.client.post("/settings", data={"csrf": tok, "section": "sources", "greenhouse": "1"}).status_code == 404
    page = w.client.get("/settings").text
    assert 'id="sources"' not in page and 'id="ai"' not in page and 'id="schedule"' not in page
    assert "/admin/users" not in w.client.get("/").text


def test_suspended_users_sessions_are_revoked(tmp_path):
    w = capture_web(tmp_path)
    d = w.db()
    bea = make_user(d, "bea")
    d.close()
    login_ok(w, "bea")
    assert w.client.get("/").status_code == 200
    d = w.db()
    users_mod.change_status(d, bea, "suspend", "user:alex")
    assert d.scalar("SELECT COUNT(*) FROM sessions WHERE user_id=?", (bea,)) == 0
    d.close()
    assert w.client.get("/", follow_redirects=False).status_code == 303
    r = w.login("bea")
    assert r.status_code == 403 and "suspended" in r.text
    d = w.db()
    users_mod.change_status(d, bea, "reactivate", "user:alex")
    d.close()
    login_ok(w, "bea")


def test_session_of_user_whose_status_changes_is_dropped(tmp_path):
    """Expected: even a session row that survives (e.g. status changed by SQL) is refused and deleted."""
    w = capture_web(tmp_path)
    d = w.db()
    make_user(d, "bea")
    d.close()
    login_ok(w, "bea")
    d = w.db()
    d.run("UPDATE users SET status='rejected' WHERE username='bea'")
    d.close()
    assert w.client.get("/", follow_redirects=False).status_code == 303
    d = w.db()
    assert d.scalar("SELECT COUNT(*) FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.username='bea'") == 0
    d.close()


def test_admin_cannot_be_changed_and_delete_needs_confirmation(tmp_path):
    w = capture_web(tmp_path)
    d = w.db()
    bea = make_user(d, "bea")
    seed_private(d, bea, "Bea")
    d.close()
    login_ok(w, TEST_USER)
    tok = w.csrf("/admin/users")
    w.client.post(f"/admin/users/{ADMIN}/suspend", data={"csrf": tok})
    assert user_status(w, "alex") == "approved"
    w.client.post(f"/admin/users/{bea}/delete", data={"csrf": tok, "confirm_username": "wrong"})
    assert user_status(w, "bea") == "approved"
    w.client.post(f"/admin/users/{bea}/delete", data={"csrf": tok, "confirm_username": "bea"})
    assert user_status(w, "bea") is None
    d = w.db()
    for t in ("applications", "resume_versions", "cover_letters", "job_scores", "prep_plans", "skill_progress",
              "user_settings", "user_bookmarks", "uploads"):
        assert d.scalar(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (bea,)) == 0, t
    assert d.scalar("SELECT COUNT(*) FROM files WHERE user_id=?", (bea,)) == 0
    assert d.scalar("SELECT COUNT(*) FROM jobs") >= 1    # shared jobs stay
    d.close()


# ----------------------------------------------------------------------------- data isolation

def seed_private(db, uid: int, tag: str) -> dict:
    """Give a user a full private job hunt (SYNTHETIC): resume (+file), variant, scored+bookmarked job, prepared
    application with cover letter, evidence upload, prep plan, skill progress and candidate settings."""
    v = store.import_bytes(db, pdf_bytes(), f"{tag}_cv.pdf", user_id=uid)
    variant = store.create_variant(db, "dotnet_backend", "IE", user_id=uid)
    jid = insert_job(db, title=f"Platform Engineer {tag}", company=f"Shared {tag} Co", score=81.5, rec="apply",
                     bookmarked=1, user_id=uid)
    db.run("UPDATE jobs SET last_verified_at=? WHERE id=?", (iso(), jid))
    a = apps.prepare(db, jid, "agent", variant["id"], f"Dear team, SECRET-LETTER-{tag}.", "template", user_id=uid)
    fid = db.insert("INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, created_at, user_id)"
                    " VALUES ('submission_evidence', 'c.png', 'image/png', 4, 'x', ?, ?, ?)",
                    (db.blob(b"\x89PNG"), iso(), uid))
    db.run("INSERT INTO uploads(application_id, file_id, created_at, user_id) VALUES (?,?,?,?)", (a["id"], fid, iso(), uid))
    db.run("INSERT INTO prep_plans(user_id, job_id, plan_json, created_at) VALUES (?,?,?,?)",
           (uid, jid, dumps({"study_plan": [{"skill": f"SECRETSKILL{tag}", "priority": "high", "topics": ["t"]}],
                             "demonstrated": [], "partial": [], "missing": []}), iso()))
    db.run("INSERT INTO skill_progress(user_id, skill, status, notes, updated_at) VALUES (?,?,?,?,?)",
           (uid, f"SECRETSKILL{tag}", "learning", f"SECRET-NOTE-{tag}", iso()))
    cand = dict(settings_mod.get(db, "candidate", uid), full_name=f"Secret Name {tag}",
                email=f"secret.{tag.lower()}@example.com")
    settings_mod.set_value(db, "candidate", cand, "test", user_id=uid)
    return {"resume": v["id"], "file": v["source_file_id"], "variant": variant["id"], "job": jid, "app": a["id"],
            "letter": a["cover_letter_id"], "evidence": fid}


SECRETS_A = ("SECRET-LETTER-A", "SECRETSKILLA", "SECRET-NOTE-A", "Secret Name A", "secret.a@example.com", "A_cv.pdf")


def _assert_clean(text: str, where: str):
    for s in SECRETS_A:
        assert s not in text, (where, s)


def test_user_b_cannot_see_or_act_on_user_a_data_via_any_route(tmp_path):
    """Expected: logged in as B, every route with A's IDs returns 404/403 (never data), every list/page/export B can
    open contains none of A's private data, and A's records are unchanged afterwards. A is the ADMIN here, so this
    also proves the admin's data is private; the reverse direction (admin vs B) is checked at the end."""
    w = capture_web(tmp_path)
    d = w.db()
    A = seed_private(d, ADMIN, "A")
    bea = make_user(d, "bea")
    before = d.one("SELECT status, row_version, answers_json, cover_letter_id FROM applications WHERE id=?", (A["app"],))
    run = agent.RunContext(d, "agent", "manual", agent_config(w.cfg.database_url), settings_mod.get_all(d, None), ADMIN)
    run.decision(A["job"], "prepared", "SECRET-LETTER-A decision", ADMIN)
    run.finish("succeeded")
    d.close()
    login_ok(w, "bea")
    c = w.client
    tok = w.csrf("/settings")
    get_routes = [f"/applications/{A['app']}", f"/letters/{A['letter']}", f"/studio?v={A['resume']}",
                  f"/studio?v={A['variant']}", f"/studio/{A['resume']}/download.pdf",
                  f"/studio/{A['resume']}/download.docx", f"/studio/{A['resume']}/download.original",
                  f"/studio/{A['variant']}/download.pdf", f"/files/{A['file']}", f"/files/{A['evidence']}",
                  f"/runs/{run.id}"]
    for path in get_routes:
        r = c.get(path)
        assert r.status_code in (403, 404), (path, r.status_code)
        _assert_clean(r.text, path)
    posts = {
        f"/applications/{A['app']}/approve": {"confirm": "yes", "fingerprint": "x"},
        f"/applications/{A['app']}/revoke": {}, f"/applications/{A['app']}/start": {},
        f"/applications/{A['app']}/outcome": {"outcome": "submitted", "reference_number": "R1"},
        f"/applications/{A['app']}/retry": {},
        f"/applications/{A['app']}/answers": {"question": "Q", "answer": "hijacked"},
        f"/applications/{A['app']}/cover-letter": {"body": "hijacked letter"},
        f"/applications/{A['app']}/progress": {"notes": "hijacked"},
        f"/applications/{A['app']}/correct": {"to_status": "withdrawn", "reason": "hijacked reason"},
        f"/applications/{A['app']}/withdraw": {},
        f"/studio/{A['variant']}/summary": {"summary": "hijacked"},
        f"/studio/{A['variant']}/status": {"status": "archived"},
        f"/studio/{A['resume']}/canonical": {"confirm": "yes"},
        f"/studio/{A['resume']}/reanalyze": {},
    }
    for path, data in posts.items():
        r = c.post(path, data={"csrf": tok, **data}, follow_redirects=False)
        assert r.status_code in (403, 404), (path, r.status_code)
        _assert_clean(r.text, path)
    pages = ["/", "/jobs", f"/jobs/{A['job']}", f"/jobs?job={A['job']}", "/applications", "/studio", "/prep",
             "/settings", "/settings/export.json", "/audit", "/audit?entity=application", "/audit?entity=settings",
             "/api/jobs", "/api/stats", "/jobs/export.csv", "/mnc", "/sponsorship", "/runs", "/jobs/compare?id=%d" % A["job"]]
    for path in pages:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        _assert_clean(r.text, path)
    # the shared job is visible to B, but with B's own (absent) score / bookmark / application
    jobs = c.get("/api/jobs").json()["jobs"]
    mine = next(j for j in jobs if j["id"] == A["job"])
    assert mine["score"] is None and mine["bookmarked"] == 0 and mine["app_id"] is None
    assert "SECRET" not in c.get(f"/jobs/{A['job']}").text
    # A's records are untouched
    d = w.db()
    after = d.one("SELECT status, row_version, answers_json, cover_letter_id FROM applications WHERE id=?", (A["app"],))
    assert after == before
    assert d.scalar("SELECT status FROM resume_versions WHERE id=?", (A["variant"],)) == "draft"
    assert d.scalar("SELECT is_canonical FROM resume_versions WHERE id=?", (A["resume"],)) == 1
    assert d.scalar("SELECT COUNT(*) FROM files WHERE user_id=?", (bea,)) == 0
    d.close()
    # B's own actions stay B's: bookmark + external application on the shared job
    c.post(f"/jobs/{A['job']}/bookmark", data={"csrf": tok})
    r = c.post(f"/jobs/{A['job']}/external", data={"csrf": tok, "applied_on": "2026-10-01", "note": "B applied"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/applications/")
    d = w.db()
    assert d.scalar("SELECT COUNT(*) FROM user_bookmarks WHERE job_id=?", (A["job"],)) == 2
    assert d.scalar("SELECT COUNT(*) FROM applications WHERE job_id=?", (A["job"],)) == 2
    assert d.scalar("SELECT status FROM applications WHERE id=?", (A["app"],)) == "awaiting_approval"
    B = seed_private(d, bea, "B")
    d.close()
    # reverse direction: the admin cannot open B's private records either
    w.logout()
    login_ok(w, TEST_USER)
    for path in (f"/applications/{B['app']}", f"/letters/{B['letter']}", f"/studio?v={B['resume']}",
                 f"/files/{B['file']}", f"/studio/{B['variant']}/download.pdf"):
        r = w.client.get(path)
        assert r.status_code in (403, 404), path
        assert "SECRET-LETTER-B" not in r.text and "Secret Name B" not in r.text
    for path in ("/admin/users", "/applications", "/studio", "/prep", "/audit", "/settings"):
        txt = w.client.get(path).text
        for s in ("SECRET-LETTER-B", "SECRETSKILLB", "SECRET-NOTE-B", "Secret Name B", "B_cv.pdf"):
            assert s not in txt, (path, s)


def test_per_user_canonical_resume_and_settings(anydb):
    """Expected: each user has their own canonical resume and settings; changing one never affects the other;
    JOBAGENT_CANDIDATE_JSON defaults apply to the admin only."""
    bea = make_user(anydb, "bea")
    a = store.import_bytes(anydb, pdf_bytes(), "a.pdf", user_id=ADMIN)
    b = store.import_bytes(anydb, pdf_bytes(), "b.pdf", user_id=bea)   # same bytes: separate copy for B
    assert a["id"] != b["id"] and a["is_canonical"] == b["is_canonical"] == 1
    assert store.canonical(anydb, user_id=ADMIN)["id"] == a["id"]
    assert store.canonical(anydb, user_id=bea)["id"] == b["id"]
    assert store.get(anydb, a["id"], user_id=bea) is None
    with pytest.raises(LookupError):
        store.set_canonical(anydb, a["id"], "user:bea", True, user_id=bea)
    assert settings_mod.get(anydb, "candidate", ADMIN)["full_name"] == "Hrithik Rajendra Dwivedi"   # env defaults
    assert settings_mod.get(anydb, "candidate", bea)["full_name"] is None
    settings_mod.set_value(anydb, "min_match_score", 80, "user:bea", user_id=bea)
    assert settings_mod.get(anydb, "min_match_score", bea) == 80
    assert settings_mod.get(anydb, "min_match_score", ADMIN) == 55
    settings_mod.set_value(anydb, "ai", dict(settings_mod.DEFAULT_SETTINGS["ai"], enabled=False), "user:alex")
    assert settings_mod.get(anydb, "ai", bea)["enabled"] is False     # global key shared
    with pytest.raises(settings_mod.SettingsError):
        settings_mod.set_value(anydb, "weights", settings_mod.DEFAULT_SETTINGS["weights"], "x")


def test_application_api_isolation(anydb):
    """Expected: every application function refuses another user's application id (not_found) on both dialects."""
    import wf
    bea = make_user(anydb, "bea")
    a = wf.prepared(anydb)
    for fn, args in ((apps.approve, (a["id"], "user:bea", "x")), (apps.revoke_approval, (a["id"], "user:bea")),
                     (apps.start_application, (a["id"], "user:bea")), (apps.retry_after_failure, (a["id"], "user:bea")),
                     (apps.update_answers, (a["id"], [], "user:bea")),
                     (apps.update_cover_letter, (a["id"], "x", "user:bea")),
                     (apps.update_progress, (a["id"], "user:bea", "withdrawn")),
                     (apps.correct_status, (a["id"], "user:bea", "withdrawn", "because")),
                     (apps.policy_approve, (a["id"],))):
        with pytest.raises(apps.WorkflowError) as ei:
            fn(anydb, *args, user_id=bea)
        assert ei.value.code == "not_found", fn.__name__
    with pytest.raises(apps.WorkflowError):
        apps.record_outcome(anydb, a["id"], "submitted", "user:bea", user_id=bea, reference_number="X")
    assert apps.active_approval(anydb, a["id"], user_id=bea) is None
    assert apps.get_app(anydb, a["id"], user_id=bea) is None
    # B may apply to the same shared job independently (one application per user and job)
    b = apps.get_or_create(anydb, a["job_id"], "user:bea", user_id=bea)
    assert b["id"] != a["id"] and b["user_id"] == bea
    assert apps.get_or_create(anydb, a["job_id"], "user:bea", user_id=bea)["id"] == b["id"]
    # B cannot attach A's resume to B's application
    with pytest.raises(apps.WorkflowError):
        apps.prepare(anydb, a["job_id"], "user:bea", a["resume_version_id"], "x", "template", user_id=bea)


def test_shared_jobs_have_separate_scores_and_bookmarks(anydb):
    bea = make_user(anydb, "bea")
    jid = insert_job(anydb, score=90, rec="apply", bookmarked=1, user_id=ADMIN)
    anydb.run("INSERT INTO job_scores(job_id, user_id, score, recommendation, breakdown_json, details_json, "
              "weights_json, created_at) VALUES (?,?,40,'skip','{}','{}','{}',?)", (jid, bea, iso()))
    f = queries.JobFilters()
    ra = {r["id"]: r for r in queries.search_jobs(anydb, f, user_id=ADMIN)[0]}
    rb = {r["id"]: r for r in queries.search_jobs(anydb, f, user_id=bea)[0]}
    assert ra[jid]["score"] == 90 and ra[jid]["bookmarked"] == 1
    assert rb[jid]["score"] == 40 and rb[jid]["bookmarked"] == 0
    assert queries.job_detail(anydb, jid, user_id=bea)["recommendation"] == "skip"
    assert queries.toggle_bookmark(anydb, jid, user_id=bea) is True
    assert queries.search_jobs(anydb, queries.JobFilters(bookmarked=True), user_id=bea)[1] == 1
    assert queries.toggle_bookmark(anydb, jid, user_id=ADMIN) is False
    assert queries.search_jobs(anydb, queries.JobFilters(bookmarked=True), user_id=ADMIN)[1] == 0


# ----------------------------------------------------------------------------- AI is admin-only

class CountingProvider(Provider):
    name = "openai"

    def __init__(self):
        self.calls = 0

    def complete_json(self, model, system, user, max_tokens):
        self.calls += 1
        return Completion('{"must_have_skills": [], "nice_to_have_skills": [], "letter": "x"}', 10, 10, model)


def test_ai_disabled_for_non_admin_even_with_key(db):
    bea = make_user(db, "bea")
    conf = dict(settings_mod.DEFAULT_SETTINGS["ai"], enabled=True)
    prov = CountingProvider()
    assert AIService(db, prov, conf, user_id=ADMIN).available
    svc = AIService(db, prov, conf, user_id=bea)
    assert not svc.available and svc.disabled_reason == ADMIN_ONLY_REASON
    assert not AIService(db, prov, conf, user_id=None).available
    with pytest.raises(Exception):
        svc.json_call("x", "s", "u")
    # an admin service is never used for another user's resume
    store.import_bytes(db, pdf_bytes(), "b.pdf", user_id=bea)
    store.create_variant(db, "dotnet_backend", "IE", ai=AIService(db, prov, conf, user_id=ADMIN), user_id=bea)
    assert prov.calls == 0


def test_ai_not_used_for_non_admin_web_routes(tmp_path, monkeypatch):
    """Expected: even with an AI provider + key configured, a normal user's studio variant never calls the AI."""
    import jobagent.web.app as webapp
    prov = CountingProvider()
    monkeypatch.setattr(webapp, "make_provider", lambda cfg: prov)
    w = capture_web(tmp_path, ai_provider="openai", openai_api_key="sk-test")
    d = w.db()
    make_user(d, "bea")
    d.close()
    login_ok(w, "bea")
    tok = w.csrf("/studio")
    r = w.client.post("/studio/upload", data={"csrf": tok}, files={"resume": ("cv.pdf", pdf_bytes(), "application/pdf")},
                      follow_redirects=False)
    assert r.status_code == 303
    r = w.client.post("/studio/variant", data={"csrf": tok, "role_family": "dotnet_backend", "country": "IE"},
                      follow_redirects=False)
    assert r.status_code == 303 and "v=" in r.headers["location"]
    assert prov.calls == 0
    assert 'id="ai"' not in w.client.get("/settings").text


# ----------------------------------------------------------------------------- agent runs per user

def test_agent_run_scores_each_approved_user_separately(anydb, monkeypatch):
    """Expected: one run discovers once, then scores the shared job for EACH approved user with their own profile
    (needs_sponsorship differs by user); pending users are skipped; decisions are recorded per user."""
    bea = make_user(anydb, "bea")
    make_user(anydb, "cara", status="pending")
    settings_mod.set_value(anydb, "sources", {"uk_sponsor_register": {"enabled": False}}, "x")
    store.import_bytes(anydb, pdf_bytes(), "a.pdf", user_id=ADMIN)
    store.import_bytes(anydb, pdf_bytes(), "b.pdf", user_id=bea)
    cand = dict(settings_mod.get(anydb, "candidate", bea), full_name="Alex Example", email="b@example.com",
                citizenship_countries=["IE"], work_authorization={"IE": "citizen"})
    settings_mod.set_value(anydb, "candidate", cand, "user:bea", user_id=bea)
    src = FakeSource(jobs=[make_raw(n=1, title="Junior .NET Developer")])
    monkeypatch.setattr(agent, "build_sources", lambda h, c: [(src, "acme")])
    res = agent.run_agent(anydb, agent_config(anydb.url), http=HttpClient(transport=no_network_transport(),
                                                                          sleep=lambda s: None))
    assert res["status"] == "succeeded", res
    assert src.calls == 1 and res["stats"]["users_processed"] == 2
    jid = anydb.scalar("SELECT id FROM jobs")
    rows = {r["user_id"]: json.loads(r["details_json"]) for r in anydb.all("SELECT * FROM job_scores WHERE job_id=?",
                                                                          (jid,))}
    assert set(rows) == {ADMIN, bea}
    assert rows[ADMIN]["sponsorship"]["needs_sponsorship"] is True
    assert rows[bea]["sponsorship"]["needs_sponsorship"] is False
    assert anydb.scalar("SELECT COUNT(*) FROM job_scores WHERE user_id=(SELECT id FROM users WHERE username='cara')") == 0
    assert anydb.scalar("SELECT COUNT(*) FROM run_decisions WHERE decision='new' AND user_id IS NULL") == 1
    assert anydb.scalar("SELECT COUNT(*) FROM prep_plans WHERE job_id=?", (jid,)) == 2
    # a second run with a time budget resumes round-robin and never re-scores unchanged profiles
    res2 = agent.run_agent(anydb, agent_config(anydb.url), discover=False,
                           http=HttpClient(transport=no_network_transport(), sleep=lambda s: None))
    assert res2["stats"]["scored"] == 0


def test_user_run_scores_only_that_user(db, monkeypatch):
    bea = make_user(db, "bea")
    store.import_bytes(db, pdf_bytes(), "a.pdf", user_id=ADMIN)
    store.import_bytes(db, pdf_bytes(), "b.pdf", user_id=bea)
    insert_job(db)
    res = agent.run_agent(db, agent_config(db.url), discover=False, user_ids=[bea], triggered_by=bea,
                          http=HttpClient(transport=no_network_transport(), sleep=lambda s: None))
    assert res["status"] == "succeeded"
    assert {r["user_id"] for r in db.all("SELECT user_id FROM job_scores")} == {bea}
    assert db.scalar("SELECT user_id FROM runs WHERE id=?", (res["run_id"],)) == bea


def test_round_robin_cursor_orders_users(db):
    from jobagent.agent import USER_CURSOR_KEY, _user_order
    db.run("INSERT INTO settings(key, value_json, updated_at) VALUES (?, '2', ?)", (USER_CURSOR_KEY, iso()))
    assert _user_order(db, [1, 2, 3, 4], use_cursor=True) == [3, 4, 1, 2]
    assert _user_order(db, [1, 2, 3, 4], use_cursor=False) == [1, 2, 3, 4]


# ----------------------------------------------------------------------------- migration 0004 backfill

def _migrate_to(db: Database, last: str) -> None:
    db._exec("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for path in sorted((MIGRATIONS_DIR / db.dialect).glob("*.sql")):
        if path.stem > last:
            break
        with db.transaction():
            for st in split_sql(path.read_text(encoding="utf-8")):
                db._exec(st)
            db._exec("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (path.stem, iso()))


def test_migration_0004_backfills_single_user_data_to_admin(anyurl):
    """Expected: an existing single-user database (schema 0003 with data) migrates in place: the account becomes the
    approved admin; every private row (resume, files, applications, letters, approvals, uploads, scores, prep,
    skills, AI usage, decisions) is owned by it; per-user settings move to user_settings while global ones stay;
    bookmarks move to user_bookmarks; the shared UK register file stays shared; the app works on the result."""
    db = Database(anyurl)
    try:
        _migrate_to(db, "0003_mnc")
        now = iso()
        uid = db.insert("INSERT INTO users(username, password_hash, created_at, password_changed_at) VALUES "
                        "('hrithik', 'x', ?, ?)", (now, now))
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES ('candidate', ?, ?)",
               (dumps(dict(settings_mod.DEFAULT_SETTINGS["candidate"], full_name="Legacy Name")), now))
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES ('min_match_score', '70', ?)", (now,))
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES ('sources', ?, ?)",
               (dumps(settings_mod.DEFAULT_SETTINGS["sources"]), now))
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES ('_discovery_cursor', '3', ?)", (now,))
        emp = db.insert("INSERT INTO employers(name, normalized_name, created_at) VALUES ('Acme', 'acme', ?)", (now,))
        jid = db.insert("INSERT INTO jobs(job_key, employer_id, title, normalized_title, first_discovered_at, "
                        "last_seen_at, bookmarked, apply_url) VALUES ('k1', ?, 'Engineer', 'engineer', ?, ?, 1, "
                        "'https://boards.greenhouse.io/acme/jobs/1')", (emp, now, now))
        fid = db.insert("INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, created_at) "
                        "VALUES ('resume_original', 'cv.pdf', 'application/pdf', 4, 'sha1', ?, ?)", (db.blob(b"%PDF"), now))
        reg = db.insert("INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, created_at) "
                        "VALUES ('uk_sponsor_register', 'r.csv', 'text/csv', 1, 'sha2', ?, ?)", (db.blob(b"x"), now))
        rid = db.insert("INSERT INTO resume_versions(label, kind, is_canonical, source_file_id, file_sha256, "
                        "structured_json, method, status, created_at) VALUES ('Original', 'original', 1, ?, 'sha1', "
                        "'{\"name\": \"Legacy\"}', 'parse', 'approved', ?)", (fid, now))
        cid = db.insert("INSERT INTO cover_letters(job_id, resume_version_id, version, body, method, created_at) "
                        "VALUES (?, ?, 1, 'Letter', 'template', ?)", (jid, rid, now))
        aid = db.insert("INSERT INTO applications(job_id, status, resume_version_id, cover_letter_id, created_at, "
                        "updated_at, auto_apply, attempts) VALUES (?, 'ready_to_apply', ?, ?, ?, ?, 0, 1)",
                        (jid, rid, cid, now, now))
        db.run("INSERT INTO approvals(application_id, fingerprint, snapshot_json, approved_at, expires_at) VALUES "
               "(?, 'fp', '{}', ?, ?)", (aid, now, now))
        db.run("INSERT INTO uploads(application_id, file_id, created_at) VALUES (?, ?, ?)", (aid, fid, now))
        db.run("INSERT INTO runs(id, kind, trigger, status, started_at, config_json) VALUES ('r1', 'agent', 'cron', "
               "'succeeded', ?, '{}')", (now,))
        db.run("INSERT INTO job_scores(job_id, run_id, score, recommendation, breakdown_json, details_json, "
               "weights_json, created_at) VALUES (?, 'r1', 77, 'apply', '{}', '{}', '{}', ?)", (jid, now))
        db.run("INSERT INTO run_decisions(run_id, job_id, decision, reason, at) VALUES ('r1', ?, 'new', 'n', ?)",
               (jid, now))
        db.run("INSERT INTO run_decisions(run_id, job_id, decision, reason, at) VALUES ('r1', ?, 'prepared', 'p', ?)",
               (jid, now))
        db.run("INSERT INTO prep_plans(job_id, plan_json, created_at) VALUES (?, '{}', ?)", (jid, now))
        db.run("INSERT INTO skill_progress(skill, status, notes, updated_at) VALUES ('Docker', 'learning', '', ?)",
               (now,))
        db.run("INSERT INTO ai_usage(provider, model, purpose, ok, at) VALUES ('openai', 'm', 'p', 1, ?)", (now,))

        assert db.migrate() == ["0004_multiuser"]

        u = db.one("SELECT * FROM users WHERE id=?", (uid,))
        assert u["role"] == "admin" and u["status"] == "approved" and u["approved_at"] == now
        for t in ("applications", "cover_letters", "resume_versions", "uploads", "job_scores", "prep_plans",
                  "skill_progress", "ai_usage"):
            assert db.scalar(f"SELECT COUNT(*) FROM {t} WHERE user_id IS NULL OR user_id <> ?", (uid,)) == 0, t
            assert db.scalar(f"SELECT COUNT(*) FROM {t}") == 1, t
        assert db.scalar("SELECT user_id FROM files WHERE id=?", (fid,)) == uid
        assert db.scalar("SELECT user_id FROM files WHERE id=?", (reg,)) is None
        assert db.scalar("SELECT user_id FROM run_decisions WHERE decision='new'") is None
        assert db.scalar("SELECT user_id FROM run_decisions WHERE decision='prepared'") == uid
        assert db.scalar("SELECT COUNT(*) FROM approvals WHERE application_id=?", (aid,)) == 1
        assert {r["key"] for r in db.all("SELECT key FROM settings")} == {"sources", "_discovery_cursor"}
        assert {r["key"] for r in db.all("SELECT key FROM user_settings WHERE user_id=?", (uid,))} == \
            {"candidate", "min_match_score"}
        assert db.scalar("SELECT COUNT(*) FROM user_bookmarks WHERE user_id=? AND job_id=?", (uid, jid)) == 1
        # the application layer sees the migrated data as the admin's
        assert settings_mod.get(db, "candidate", uid)["full_name"] == "Legacy Name"
        assert settings_mod.get(db, "min_match_score", uid) == 70
        assert apps.get_app(db, aid, user_id=uid)["status"] == "ready_to_apply"
        assert store.canonical(db, user_id=uid)["id"] == rid
        det = queries.job_detail(db, jid, user_id=uid)
        assert det["bookmarked"] == 1 and det["score"] == 77 and det["app_id"] == aid
        # new constraints: one application per user+job, one canonical resume per user
        bea = make_user(db, "bea")
        apps.get_or_create(db, jid, "user:bea", user_id=bea)
        store_row = db.insert("INSERT INTO resume_versions(label, kind, is_canonical, structured_json, method, status, "
                              "created_at, user_id) VALUES ('B', 'original', 1, '{}', 'parse', 'approved', ?, ?)",
                              (now, bea))
        assert store_row
        from jobagent.db import IntegrityError
        with pytest.raises(IntegrityError):
            db.insert("INSERT INTO resume_versions(label, kind, is_canonical, structured_json, method, status, "
                      "created_at, user_id) VALUES ('B2', 'original', 1, '{}', 'parse', 'approved', ?, ?)", (now, bea))
        with pytest.raises(IntegrityError):
            db.insert("INSERT INTO applications(job_id, user_id, status, created_at, updated_at) VALUES "
                      "(?, ?, 'shortlisted', ?, ?)", (jid, uid, now, now))
        if db.dialect == "sqlite":
            assert db.all("PRAGMA foreign_key_check") == []
            assert db.scalar("PRAGMA foreign_keys") == 1
    finally:
        db.close()


def test_orphan_rows_adopted_by_first_admin(tmp_path):
    """Expected: rows created before any account existed (e.g. a CLI import) become the first admin's."""
    db = open_db("sqlite:///" + str(tmp_path / "o.db"))
    try:
        rid = db.insert("INSERT INTO resume_versions(label, kind, is_canonical, structured_json, method, status, "
                        "created_at) VALUES ('Orphan', 'original', 1, '{}', 'parse', 'approved', ?)", (iso(),))
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES ('min_match_score', '65', ?)", (iso(),))
        uid = ensure_admin(db)
        assert store.canonical(db, user_id=uid)["id"] == rid
        assert settings_mod.get(db, "min_match_score", uid) == 65
        assert db.scalar("SELECT COUNT(*) FROM settings WHERE key='min_match_score'") == 0
    finally:
        db.close()
