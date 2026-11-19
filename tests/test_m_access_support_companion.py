"""M. Per-user AI access (administrator grants/revokes, new sign-ups start without it), Help & Support (tickets,
suggested answers, account self-check) and the Application Companion (tokens, JSON API, form planning, learning,
the browser extension package and the in-page filler in a real Chromium). All data is SYNTHETIC."""
from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import timedelta

import pytest

from conftest import TEST_PASSWORD, extract_csrf, web_factory
from factories import ADMIN, make_user, pdf_bytes
from jobagent import answers as answers_mod
from jobagent import companion
from jobagent import helpdesk
from jobagent import settings as settings_mod
from jobagent import users as users_mod
from jobagent.ai import NO_ACCESS_REASON, AIError, AIService, BudgetExceeded, Completion, Provider, ai_access
from jobagent.db import audit, iso, utcnow
from jobagent.mailer import Mailer, Message
from jobagent.mailer import Provider as MailProvider
from jobagent.resume import store
from webdata import insert_job

AI_CONF = dict(settings_mod.DEFAULT_SETTINGS["ai"], enabled=True)


class FakeAI(Provider):
    name = "openai"

    def __init__(self, text='{"answer": "I build Python services and enjoy payments work."}'):
        self.calls, self.text = 0, text

    def complete_json(self, model, system, user, max_tokens):
        self.calls += 1
        return Completion(self.text, 100, 50, model)


class Capture(MailProvider):
    name = "capture"

    def __init__(self):
        self.sent: list[Message] = []

    def send(self, msg, sender):
        self.sent.append(msg)


def web(tmp_path, **cfg):
    prov = Capture()
    w = web_factory(tmp_path, mailer=Mailer(prov, "JobAgent <t@example.com>"), **cfg)
    w.mail = prov
    w.setup_user()                      # admin "alex" = id 1
    assert w.login().status_code == 303
    return w


def login(w, username):
    w.client.cookies.clear()
    r = w.login(username, TEST_PASSWORD)
    assert r.status_code == 303, r.text[:300]


def flash_of(r) -> str:
    return r.headers.get("location", "")


# =============================================================================== AI ACCESS

def test_new_signup_has_no_ai_and_admin_always_has(anydb):
    db = anydb
    bea = make_user(db, "bea")
    assert ai_access(db, ADMIN)["allowed"] is True
    acc = ai_access(db, bea)
    assert acc == {"allowed": False, "limit": None, "spent": 0.0}
    svc = AIService(db, FakeAI(), AI_CONF, user_id=bea)
    assert not svc.available and svc.disabled_reason == NO_ACCESS_REASON
    assert ai_access(db, None)["allowed"] is False and ai_access(db, 9999)["allowed"] is False


def test_grant_then_revoke_takes_effect_immediately(anydb):
    db = anydb
    bea = make_user(db, "bea")
    prov = FakeAI('{"x": 1}')
    users_mod.set_ai_access(db, bea, True, "user:alex", actor_id=ADMIN)
    svc = AIService(db, prov, AI_CONF, user_id=bea)
    assert svc.available
    assert svc.json_call("p", "s", "u1") == {"x": 1} and prov.calls == 1
    users_mod.set_ai_access(db, bea, False, "user:alex", actor_id=ADMIN)
    with pytest.raises(AIError):                       # the SAME service object stops working after revoke
        svc.json_call("p", "s", "u2")
    assert prov.calls == 1 and svc.disabled_reason == NO_ACCESS_REASON
    actions = [r["action"] for r in db.all("SELECT action FROM audit_events WHERE entity='admin' ORDER BY id")]
    assert actions[-2:] == ["ai_grant", "ai_revoke"]


@pytest.mark.parametrize("status", ["pending", "rejected", "suspended"])
def test_cannot_grant_ai_to_unapproved_accounts(anydb, status):
    db = anydb
    uid = make_user(db, "cara", status=status)
    with pytest.raises(users_mod.AccountError, match="Approve the account"):
        users_mod.set_ai_access(db, uid, True, "x")


def test_ai_grant_edge_cases(anydb):
    db = anydb
    bea = make_user(db, "bea")
    with pytest.raises(users_mod.AccountError, match="always has AI"):
        users_mod.set_ai_access(db, ADMIN, False, "x")
    with pytest.raises(users_mod.AccountError, match="not found"):
        users_mod.set_ai_access(db, 4242, True, "x")
    for bad in (-1, 1000.01):
        with pytest.raises(users_mod.AccountError, match="between 0 and 1000"):
            users_mod.set_ai_access(db, bea, True, "x", monthly_limit_usd=bad)
    u = users_mod.set_ai_access(db, bea, True, "x", monthly_limit_usd=2.555)
    assert u["ai_access"] == 1 and u["ai_monthly_limit_usd"] == 2.56 or u["ai_monthly_limit_usd"] == 2.55
    u = users_mod.set_ai_access(db, bea, False, "x")
    assert u["ai_access"] == 0 and u["ai_monthly_limit_usd"] is None


def test_suspended_user_loses_ai_even_with_grant(anydb):
    db = anydb
    bea = make_user(db, "bea")
    users_mod.set_ai_access(db, bea, True, "x")
    users_mod.change_status(db, bea, "suspend", "x")
    assert ai_access(db, bea)["allowed"] is False
    users_mod.change_status(db, bea, "reactivate", "x")
    assert ai_access(db, bea)["allowed"] is True          # grant survives a suspension


def test_personal_monthly_allowance_is_enforced_per_user(anydb):
    db = anydb
    bea, cara = make_user(db, "bea"), make_user(db, "cara")
    users_mod.set_ai_access(db, bea, True, "x", monthly_limit_usd=0.01)
    users_mod.set_ai_access(db, cara, True, "x")
    with db.transaction():
        db.run("INSERT INTO ai_usage(provider, model, purpose, cost_usd, ok, at, user_id) VALUES "
               "('openai','m','p',0.02,1,?,?)", (iso(), bea))
    prov = FakeAI('{"ok": true}')
    with pytest.raises(BudgetExceeded, match="allowance"):
        AIService(db, prov, AI_CONF, user_id=bea).json_call("p", "s", "u")
    assert AIService(db, prov, AI_CONF, user_id=cara).json_call("p", "s", "u") == {"ok": True}
    # last month's spend does not count
    with db.transaction():
        db.run("UPDATE ai_usage SET at=? WHERE user_id=?", (iso(utcnow() - timedelta(days=40)), bea))
    assert AIService(db, prov, AI_CONF, user_id=bea).json_call("p", "s", "u-new") == {"ok": True}


def test_web_admin_grants_and_revokes_ai(tmp_path):
    w = web(tmp_path)
    d = w.db()
    bea = make_user(d, "bea")
    d.close()
    tok = w.csrf("/admin/users")
    page = w.client.get("/admin/users").text
    assert "AI access" in page and "Grant AI access" in page
    # the specific route is not swallowed by /admin/users/{id}/{action}
    r = w.client.post(f"/admin/users/{bea}/ai-access", data={"csrf": tok, "grant": "1", "monthly_limit_usd": "3"},
                      follow_redirects=False)
    assert r.status_code == 303
    d = w.db()
    assert ai_access(d, bea) == {"allowed": True, "limit": 3.0, "spent": 0.0}
    d.close()
    assert any(m.to == "bea@example.com" and "enabled" in m.subject for m in w.mail.sent)
    assert "$3.00" in [m for m in w.mail.sent if m.to == "bea@example.com"][-1].text
    # bad input
    for bad in ("abc", "nan", "-5", "5000"):
        r = w.client.post(f"/admin/users/{bea}/ai-access", data={"csrf": tok, "grant": "1", "monthly_limit_usd": bad},
                          follow_redirects=True)
        assert r.status_code == 200 and ("must be a number" in r.text or "between 0 and 1000" in r.text), bad
    assert w.client.post("/admin/users/999/ai-access", data={"csrf": tok, "grant": "1"}).status_code == 404
    assert w.client.post(f"/admin/users/{bea}/ai-access", data={"grant": "1"}).status_code == 403   # no CSRF
    # open redirect via next is neutralised
    r = w.client.post(f"/admin/users/{bea}/ai-access", data={"csrf": tok, "grant": "0", "next": "//evil.example"},
                      follow_redirects=False)
    assert r.headers["location"] == "/"
    d = w.db()
    assert ai_access(d, bea)["allowed"] is False
    d.close()


def test_non_admin_cannot_change_ai_access(tmp_path):
    w = web(tmp_path)
    d = w.db()
    bea, cara = make_user(d, "bea"), make_user(d, "cara")
    d.close()
    login(w, "bea")
    tok = w.csrf("/settings")
    for target in (bea, cara):
        r = w.client.post(f"/admin/users/{target}/ai-access", data={"csrf": tok, "grant": "1"})
        assert r.status_code == 404
    d = w.db()
    assert not ai_access(d, bea)["allowed"] and not ai_access(d, cara)["allowed"]
    d.close()


def test_granted_user_web_routes_use_ai(tmp_path, monkeypatch):
    """With a grant, a normal user's studio variant may call the AI (the fact guard still applies)."""
    import jobagent.web.app as webapp
    prov = FakeAI('{"summary": "x"}')
    monkeypatch.setattr(webapp, "make_provider", lambda cfg: prov)
    w = web(tmp_path, ai_provider="openai", openai_api_key="sk-test")
    d = w.db()
    bea = make_user(d, "bea")
    d.close()
    login(w, "bea")
    assert "Request AI access" in w.client.get("/settings").text
    d = w.db()
    users_mod.set_ai_access(d, bea, True, "x")
    d.close()
    assert "Request AI access" not in w.client.get("/settings").text
    tok = w.csrf("/studio")
    w.client.post("/studio/upload", data={"csrf": tok}, files={"resume": ("cv.pdf", pdf_bytes(), "application/pdf")})
    w.client.post("/studio/variant", data={"csrf": tok, "role_family": "dotnet_backend", "country": "IE"})
    d = w.db()
    used = d.scalar("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (bea,))
    d.close()
    assert prov.calls >= 1 and used >= 1


# =============================================================================== HELP DESK: answers + checks

def test_suggest_finds_the_right_builtin_answer(anydb):
    db = anydb
    s = helpdesk.suggest(db, "auto apply keeps asking me to fill the form manually")
    assert s and s[0]["id"] == "kb-manual-forms"
    assert helpdesk.suggest(db, "how do I fill a Workday application form")[0]["category"] == "companion"
    assert helpdesk.suggest(db, "how do I get AI access")[0]["id"] == "kb-ai-access"
    assert helpdesk.suggest(db, "") == []
    assert helpdesk.suggest(db, "zzqx plorf") == []
    assert all(x["category"] == "account" for x in helpdesk.suggest(db, "", "account"))
    # punctuation / casing / HTML in queries are harmless
    assert helpdesk.suggest(db, "<script>CAPTCHA!!!</script>")[0]["id"] == "kb-captcha"


def test_team_answers_rank_first_and_helpful_counts(anydb):
    db = anydb
    aid = helpdesk.save_answer(db, actor="x", admin_id=ADMIN, category="auto_apply",
                               question="Auto-apply skipped my Greenhouse job",
                               answer="Jobs below your minimum score are skipped; lower it in Settings.",
                               keywords="skipped greenhouse score")
    top = helpdesk.suggest(db, "greenhouse job skipped by auto-apply")[0]
    assert top["id"] == f"team-{aid}" and top["source"] == "team"
    helpdesk.mark_helpful(db, f"team-{aid}")
    helpdesk.mark_helpful(db, "kb-captcha")                 # built-in ids are ignored
    helpdesk.mark_helpful(db, "team-abc; DROP TABLE x")      # garbage ignored
    assert db.scalar("SELECT helpful FROM support_answers WHERE id=?", (aid,)) == 1
    helpdesk.save_answer(db, actor="x", admin_id=ADMIN, category="nope", question="Edited question here",
                         answer="Edited answer text.", answer_id=aid)
    assert db.one("SELECT category, question FROM support_answers WHERE id=?", (aid,)) == \
        {"category": "other", "question": "Edited question here"}
    with pytest.raises(helpdesk.SupportError):
        helpdesk.save_answer(db, actor="x", admin_id=ADMIN, category="x", question="q", answer="short")
    with pytest.raises(helpdesk.SupportError, match="not found"):
        helpdesk.save_answer(db, actor="x", admin_id=ADMIN, category="x", question="Valid question",
                             answer="A valid answer text.", answer_id=777)
    helpdesk.delete_answer(db, aid, actor="x")
    assert helpdesk.flagged_answers(db) == []


def test_account_check_explains_the_users_setup(anydb):
    db = anydb
    bea = make_user(db, "bea")
    titles = {c["title"] for c in helpdesk.diagnose(db, bea)}
    assert {"No resume uploaded", "Candidate details incomplete", "Auto-apply is off",
            "AI features are not enabled for your account", "Application Companion not connected"} <= titles
    store.import_bytes(db, pdf_bytes(), "b.pdf", user_id=bea)
    settings_mod.set_value(db, "candidate", {"full_name": "Bea Example", "email": "bea@example.com",
                                             "phone": "+353 85 123 4567", "current_location": "Dublin, Ireland"},
                           "x", user_id=bea)
    users_mod.set_ai_access(db, bea, True, "x", monthly_limit_usd=1)
    companion.create_token(db, user_id=bea, name="Laptop", actor="x")
    titles = {c["title"] for c in helpdesk.diagnose(db, bea)}
    assert "No resume uploaded" not in titles and "Candidate details incomplete" not in titles
    assert "Notice period / salary not set" in titles
    assert "Application Companion not connected" not in titles and not any("AI features" in t for t in titles)
    # a hand-back with an unanswered question shows up, and the consent warning appears for consent questions
    jid = insert_job(db, user_id=bea, app_status="awaiting_approval")
    app_id = db.scalar("SELECT id FROM applications WHERE job_id=? AND user_id=?", (jid, bea))
    audit(db, actor="autoapply", entity="application", entity_id=app_id, action="autoapply_handback",
          detail={"code": "unknown_questions",
                  "unknown_questions": ["Do you have a driving licence?", "I agree to the privacy policy"]})
    checks = {c["title"]: c for c in helpdesk.diagnose(db, bea)}
    assert "2 screening question(s) need an answer" in checks
    assert "Required privacy boxes are not allowed" in checks
    # AI allowance nearly used
    with db.transaction():
        db.run("INSERT INTO ai_usage(provider, model, purpose, cost_usd, ok, at, user_id) VALUES "
               "('openai','m','p',0.95,1,?,?)", (iso(), bea))
    assert "AI allowance almost used" in {c["title"] for c in helpdesk.diagnose(db, bea)}


# =============================================================================== HELP DESK: tickets

def test_ticket_validation_and_limits(anydb):
    db = anydb
    bea = make_user(db, "bea")
    with pytest.raises(helpdesk.SupportError, match="subject"):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject="ab", body="long enough body", actor="x")
    with pytest.raises(helpdesk.SupportError, match="subject"):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject="x" * 151, body="long enough body", actor="x")
    with pytest.raises(helpdesk.SupportError, match="Describe"):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject="Valid subject", body="short", actor="x")
    with pytest.raises(helpdesk.SupportError, match="Describe"):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject="Valid subject", body="y" * 5001, actor="x")
    tid = helpdesk.create_ticket(db, user_id=bea, category="made-up", subject="  Many   spaces  here ",
                                 body="Body with \x00 null byte inside", actor="x")
    t = helpdesk.get_ticket(db, tid, user_id=bea)
    assert t["category"] == "other" and t["subject"] == "Many spaces here" and "\x00" not in t["messages"][0]["body"]
    for i in range(helpdesk.MAX_OPEN_PER_USER - 1):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject=f"Problem {i}", body="Something broke here",
                               actor="x")
    with pytest.raises(helpdesk.SupportError, match="already have"):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject="One more", body="Something broke here",
                               actor="x")
    # resolving frees a slot; the daily cap still applies
    for t in helpdesk.list_for_user(db, bea):
        helpdesk.set_status(db, t["id"], "resolved", actor="x", user_id=bea)
    for i in range(helpdesk.MAX_PER_DAY - helpdesk.MAX_OPEN_PER_USER):
        tid = helpdesk.create_ticket(db, user_id=bea, category="bug", subject=f"Later {i}", body="Something broke",
                                     actor="x")
        helpdesk.set_status(db, tid, "resolved", actor="x", user_id=bea)
    with pytest.raises(helpdesk.SupportError, match="today"):
        helpdesk.create_ticket(db, user_id=bea, category="bug", subject="Too many", body="Something broke here",
                               actor="x")


def test_ticket_conversation_status_and_unread(anydb):
    db = anydb
    bea, cara = make_user(db, "bea"), make_user(db, "cara")
    tid = helpdesk.create_ticket(db, user_id=bea, category="auto_apply", subject="Forms are manual",
                                 body="Auto-apply asks me to fill forms", actor="x")
    assert helpdesk.unread_for_user(db, bea) == 0
    helpdesk.reply(db, tid, author_id=ADMIN, staff=True, body="Answer the screening questions.", actor="admin")
    assert helpdesk.get_ticket(db, tid, user_id=bea)["status"] == "answered"
    assert helpdesk.unread_for_user(db, bea) == 1
    helpdesk.mark_seen(db, tid, bea)
    assert helpdesk.unread_for_user(db, bea) == 0
    helpdesk.reply(db, tid, author_id=bea, staff=False, body="Thanks, still stuck", actor="x")
    assert helpdesk.get_ticket(db, tid, user_id=bea)["status"] == "open"
    # isolation: cara can't see, reply to or resolve bea's ticket
    assert helpdesk.get_ticket(db, tid, user_id=cara) is None
    with pytest.raises(helpdesk.SupportError, match="not found"):
        helpdesk.reply(db, tid, author_id=cara, staff=False, body="hijack", actor="x")
    with pytest.raises(helpdesk.SupportError, match="not found"):
        helpdesk.set_status(db, tid, "resolved", actor="x", user_id=cara)
    # users can only resolve / re-open; the admin can close
    with pytest.raises(helpdesk.SupportError, match="resolved or re-open"):
        helpdesk.set_status(db, tid, "closed", actor="x", user_id=bea)
    with pytest.raises(helpdesk.SupportError, match="Unknown status"):
        helpdesk.set_status(db, tid, "deleted", actor="x", user_id=None)
    helpdesk.reply(db, tid, author_id=ADMIN, staff=True, body="Fixed now.", actor="admin", resolve=True)
    assert helpdesk.get_ticket(db, tid, user_id=None)["status"] == "resolved"
    helpdesk.set_status(db, tid, "closed", actor="admin", user_id=None)
    with pytest.raises(helpdesk.SupportError, match="closed"):
        helpdesk.reply(db, tid, author_id=bea, staff=False, body="again", actor="x")
    with pytest.raises(helpdesk.SupportError, match="closed"):
        helpdesk.set_status(db, tid, "open", actor="x", user_id=bea)
    with pytest.raises(helpdesk.SupportError):
        helpdesk.reply(db, tid, author_id=ADMIN, staff=True, body="", actor="admin")
    assert [m["is_staff"] for m in helpdesk.get_ticket(db, tid, user_id=None)["messages"]] == [0, 1, 0, 1]
    assert helpdesk.counts(db)["closed"] == 1
    assert [t["id"] for t in helpdesk.list_all(db, "closed", q="manual")] == [tid]
    assert helpdesk.list_all(db, "active") == []
    assert helpdesk.list_all(db, "all", q="bea")[0]["username"] == "bea"


def test_deleting_a_user_removes_their_tickets_and_tokens(anydb):
    db = anydb
    bea = make_user(db, "bea")
    tid = helpdesk.create_ticket(db, user_id=bea, category="bug", subject="Broken page", body="It broke today",
                                 actor="x")
    aid = helpdesk.save_answer(db, actor="x", admin_id=ADMIN, category="bug", question="Broken page question",
                               answer="Reload the page please.", source_ticket_id=tid)
    companion.create_token(db, user_id=bea, name="x", actor="x")
    users_mod.delete_user(db, bea, "admin")
    assert db.scalar("SELECT COUNT(*) FROM support_tickets") == 0
    assert db.scalar("SELECT COUNT(*) FROM support_messages") == 0
    assert db.scalar("SELECT COUNT(*) FROM api_tokens") == 0
    assert db.scalar("SELECT source_ticket_id FROM support_answers WHERE id=?", (aid,)) is None   # answer kept


def test_web_support_end_to_end(tmp_path):
    w = web(tmp_path)
    d = w.db()
    make_user(d, "bea")
    make_user(d, "cara")
    d.close()
    login(w, "bea")
    page = w.client.get("/support").text
    assert "Your account check" in page and "No resume uploaded" in page and "Request AI access" in page
    r = w.client.get("/support", params={"q": "auto apply asks me to fill forms manually"})
    assert "Why does auto-apply still ask me to fill application forms manually?" in r.text
    assert w.client.get("/support/suggest", params={"q": "captcha"}).json()["suggestions"][0]["id"] == "kb-captcha"
    assert w.client.get("/support/suggest", params={"q": "x" * 5000, "category": "<bad>"}).status_code == 200
    tok = extract_csrf(page)
    r = w.client.post("/support", data={"csrf": tok, "category": "companion", "subject": "<script>alert(1)</script>",
                                        "body": "Workday form did not fill <b>at all</b>"}, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    t_page = w.client.get(f"/support/{tid}").text
    assert "<script>alert(1)</script>" not in t_page and "&lt;script&gt;" in t_page
    assert any(m.to == "owner@example.com" and "[JobAgent support]" in m.subject for m in w.mail.sent)
    assert w.client.post("/support", data={"category": "bug", "subject": "No csrf", "body": "xxxxxxxxxxxx"}
                         ).status_code == 403
    r = w.client.post("/support", data={"csrf": tok, "category": "bug", "subject": "x", "body": "short"},
                      follow_redirects=True)
    assert "subject" in r.text
    # AI request straight from the AI card
    r = w.client.post("/support", data={"csrf": tok, "category": "ai_access", "subject": "Request: AI access",
                                        "body": "Please enable AI features for my account."}, follow_redirects=False)
    ai_tid = int(r.headers["location"].rsplit("/", 1)[1])

    # another user cannot read or act on it, and admin pages are hidden
    login(w, "cara")
    tok_c = w.csrf("/support")
    assert w.client.get(f"/support/{tid}").status_code == 404
    assert w.client.post(f"/support/{tid}/reply", data={"csrf": tok_c, "body": "hijack"}).status_code == 404
    assert w.client.post(f"/support/{tid}/status", data={"csrf": tok_c, "status": "resolved"}).status_code == 404
    assert w.client.get("/admin/support").status_code == 404
    assert w.client.get(f"/admin/support/{tid}").status_code == 404
    assert w.client.post(f"/admin/support/{tid}/reply", data={"csrf": tok_c, "body": "x"}).status_code == 404
    assert w.client.post("/admin/support/answers", data={"csrf": tok_c, "question": "q", "answer": "a"}
                         ).status_code == 404

    # admin: desk, reply + save as suggested answer, grant AI from the request
    login(w, "alex")
    desk = w.client.get("/admin/support").text
    assert "&lt;script&gt;" in desk and "Request: AI access" in desk
    assert 'class="nav-count"' in desk                          # open requests badge
    tp = w.client.get(f"/admin/support/{tid}").text
    assert "Suggested replies" in tp and "Grant AI access" in tp
    tok_a = extract_csrf(tp)
    r = w.client.post(f"/admin/support/{tid}/reply", data={
        "csrf": tok_a, "body": "Use Fill this form again after the page fully loads.", "save_answer": "1",
        "answer_question": "Companion did not fill a Workday form", "answer_keywords": "workday fill companion"},
        follow_redirects=False)
    assert r.status_code == 303
    assert any(m.to == "bea@example.com" and m.subject.startswith("Re:") for m in w.mail.sent)
    r = w.client.post(f"/admin/users/2/ai-access", data={"csrf": tok_a, "grant": "1", "next": f"/admin/support/{ai_tid}"},
                      follow_redirects=False)
    assert r.headers["location"] == f"/admin/support/{ai_tid}"
    assert w.client.post(f"/admin/support/{ai_tid}/status", data={"csrf": tok_a, "status": "bogus"},
                         follow_redirects=True).status_code == 200
    assert w.client.post("/admin/support/9999/status", data={"csrf": tok_a, "status": "closed"}).status_code == 404
    assert w.client.get("/admin/support", params={"status": "nonsense", "category": "x", "q": "%' OR 1=1 --"}
                        ).status_code == 200

    # user sees the reply badge, the reply, and the flagged answer now helps others
    login(w, "bea")
    assert 'class="nav-count"' in w.client.get("/applications").text
    tp = w.client.get(f"/support/{tid}").text
    assert "Use Fill this form again" in tp
    assert 'class="nav-count"' not in w.client.get("/applications").text       # seen
    login(w, "cara")
    r = w.client.get("/support", params={"q": "workday form did not fill"})
    assert "Companion did not fill a Workday form" in r.text and "Answered by the team" in r.text
    login(w, "bea")
    assert "AI features" in w.client.get("/support").text and "Enabled" in w.client.get("/support").text


# =============================================================================== COMPANION: tokens

def test_token_lifecycle_and_authentication(anydb):
    db = anydb
    bea = make_user(db, "bea")
    token, row = companion.create_token(db, user_id=bea, name="  My   Laptop  ", actor="x")
    assert token.startswith("jac_") and len(token) > 40 and row["name"] == "My Laptop"
    assert db.scalar("SELECT COUNT(*) FROM api_tokens WHERE token_hash=?", (token,)) == 0     # only the hash
    assert companion.authenticate(db, f"Bearer {token}")["id"] == bea
    for bad in (None, "", token, f"Basic {token}", "Bearer jac_short", "Bearer " + token + "x",
                "Bearer " + "x" * 300, "Bearer abcdefghijklmnop"):
        assert companion.authenticate(db, bad) is None, bad
    # expired
    with db.transaction():
        db.run("UPDATE api_tokens SET expires_at=? WHERE id=?", (iso(utcnow() - timedelta(seconds=1)), row["id"]))
    assert companion.authenticate(db, f"Bearer {token}") is None
    assert companion.list_tokens(db, bea)[0]["active"] is False
    # suspended user
    t2, r2 = companion.create_token(db, user_id=bea, name="", actor="x")
    assert r2["name"] == "Browser"
    users_mod.change_status(db, bea, "suspend", "x")
    assert companion.authenticate(db, f"Bearer {t2}") is None
    users_mod.change_status(db, bea, "reactivate", "x")
    assert companion.authenticate(db, f"Bearer {t2}")["id"] == bea
    # revoke (own only)
    cara = make_user(db, "cara")
    with pytest.raises(companion.CompanionError):
        companion.revoke_token(db, r2["id"], user_id=cara, actor="x")
    companion.revoke_token(db, r2["id"], user_id=bea, actor="x")
    assert companion.authenticate(db, f"Bearer {t2}") is None
    with pytest.raises(companion.CompanionError):
        companion.revoke_token(db, r2["id"], user_id=bea, actor="x")           # already revoked
    # max active tokens
    for i in range(companion.MAX_ACTIVE_TOKENS):
        companion.create_token(db, user_id=bea, name=f"t{i}", actor="x")
    with pytest.raises(companion.CompanionError, match="active tokens"):
        companion.create_token(db, user_id=bea, name="one too many", actor="x")


def test_companion_page_shows_token_once(tmp_path):
    w = web(tmp_path)
    page = w.client.get("/companion").text
    assert "Download extension" in page and "Create token" in page
    tok = extract_csrf(page)
    r = w.client.post("/companion/tokens", data={"csrf": tok, "name": "Work laptop"})
    assert r.status_code == 200 and "copy it now" in r.text
    import re
    token = re.search(r'value="(jac_[^"]+)"', r.text).group(1)
    again = w.client.get("/companion").text
    assert token not in again and token[:10] in again and "Work laptop" in again
    tid = w.db().scalar("SELECT id FROM api_tokens")
    r = w.client.post(f"/companion/tokens/{tid}/revoke", data={"csrf": tok}, follow_redirects=True)
    assert "Revoked" in r.text
    r = w.client.post(f"/companion/tokens/{tid}/revoke", data={"csrf": tok}, follow_redirects=True)
    assert "not found" in r.text.lower()
    assert w.client.post("/companion/tokens", data={"name": "x"}).status_code == 403            # no CSRF


def test_extension_package_is_complete(tmp_path):
    w = web(tmp_path)
    r = w.client.get("/companion/extension.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = {n.split("/", 1)[1] for n in z.namelist()}
    manifest = json.loads(z.read("jobagent-companion/manifest.json"))
    assert manifest["manifest_version"] == 3
    needed = {manifest["background"]["service_worker"], manifest["action"]["default_popup"], manifest["options_page"],
              *manifest["icons"].values(), "extract.js", "fill.js", "defaults.js", "popup.js", "popup.css",
              "options.js"}
    assert needed <= names, needed - names
    assert "submit" not in manifest["permissions"]
    assert b"globalThis.__jaExtract = () =>" in z.read("jobagent-companion/extract.js")
    assert z.read("jobagent-companion/defaults.js") == b'self.JA_DEFAULT_SERVER = "http://localhost:8765";\n'
    # every file the pages load exists in the package
    import re
    for page in ("popup.html", "options.html"):
        html = z.read(f"jobagent-companion/{page}").decode()
        for src in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert src in names, (page, src)
    # anonymous users can't download it
    w.client.cookies.clear()
    assert w.client.get("/companion/extension.zip", follow_redirects=False).status_code == 303


# =============================================================================== COMPANION: API

@pytest.fixture
def capi(tmp_path):
    """Web app + two users (bea, cara) with tokens; bea has a resume, candidate facts and a prepared application."""
    w = web(tmp_path)
    d = w.db()
    bea, cara = make_user(d, "bea"), make_user(d, "cara")
    settings_mod.set_value(d, "candidate", {
        "full_name": "Bea Example", "email": "bea@example.com", "phone": "+353 85 123 4567",
        "linkedin_url": "https://www.linkedin.com/in/bea-example", "current_location": "Dublin, Ireland",
        "notice_period": "1 month", "work_authorization": {"IE": "work_permit"}}, "x", user_id=bea)
    settings_mod.set_value(d, "answer_library", [{"question": "Why do you want to work at Acme?",
                                                  "answer": "I like building payment systems."}], "x", user_id=bea)
    store.import_bytes(d, pdf_bytes(), "bea.pdf", user_id=bea)
    jid = insert_job(d, title="Backend Engineer", company="Acme", country="IE",
                     apply_url="https://boards.greenhouse.io/acme/jobs/4455667", user_id=bea, app_status="ready_to_apply")
    app_id = d.scalar("SELECT id FROM applications WHERE job_id=?", (jid, ))
    cjid = insert_job(d, title="Other", company="Globex", user_id=cara, app_status="ready_to_apply")
    cara_app = d.scalar("SELECT id FROM applications WHERE job_id=?", (cjid,))
    tb, _ = companion.create_token(d, user_id=bea, name="b", actor="x")
    tc, _ = companion.create_token(d, user_id=cara, name="c", actor="x")
    d.close()
    w.client.cookies.clear()                                         # API must not depend on a session cookie
    return {"w": w, "c": w.client, "bea": bea, "cara": cara, "app": app_id, "cara_app": cara_app, "jid": jid,
            "hb": {"Authorization": f"Bearer {tb}"}, "hc": {"Authorization": f"Bearer {tc}"}}


FORM = [
    {"key": "f1", "label": "First Name*", "kind": "text", "required": True},
    {"key": "f2", "label": "Last Name", "kind": "text", "required": True},
    {"key": "f3", "label": "Email", "kind": "email", "required": True},
    {"key": "f4", "label": "Phone", "kind": "tel", "required": True},
    {"key": "f5", "label": "LinkedIn Profile", "kind": "url"},
    {"key": "f6", "label": "Will you now or in the future require visa sponsorship to work in Ireland?",
     "kind": "select", "required": True, "options": ["Yes", "No"]},
    {"key": "f7", "label": "Gender", "kind": "select", "options": ["Male", "Female", "Decline to self-identify"]},
    {"key": "f8", "label": "Why do you want to work at Acme?", "kind": "textarea", "required": True},
    {"key": "f9", "label": "What is your favourite colour?", "kind": "text", "required": True},
    {"key": "f10", "label": "Resume/CV", "kind": "file", "required": True, "hint": "resume"},
    {"key": "f11", "label": "I have read and agree to the privacy policy", "kind": "checkbox", "required": True},
    {"key": "f12", "label": "Password", "kind": "text", "required": True},
    {"key": "f13", "label": "Social Security Number", "kind": "text"},
    {"key": "f14", "label": "Notice period", "kind": "text"},
]


def test_api_requires_a_valid_token(capi):
    c = capi["c"]
    for path in ("/api/companion/me", "/api/companion/applications", "/api/companion/resume"):
        r = c.get(path)
        assert r.status_code == 401 and "token" in r.json()["error"]
        assert c.get(path, headers={"Authorization": "Bearer jac_" + "A" * 43}).status_code == 401
    assert c.post("/api/companion/plan", json={"fields": []}).status_code == 401
    # a session cookie alone is NOT enough (the API is token-only)
    capi["w"].login("alex", TEST_PASSWORD)
    assert c.get("/api/companion/me").status_code == 401
    r = c.get("/api/companion/me", headers=capi["hb"])
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"] == "bea" and body["ai"]["allowed"] is False
    assert body["profile"]["has_resume"] and "salary_expectation" in body["profile"]["missing"]
    assert r.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}       # no CORS for web pages


def test_api_matches_the_page_to_the_users_application(capi):
    c, h = capi["c"], capi["hb"]
    for url in ("https://boards.greenhouse.io/acme/jobs/4455667", "https://boards.greenhouse.io/acme/jobs/4455667/",
                "https://boards.greenhouse.io/acme/jobs/4455667/apply", "https://www.boards.greenhouse.io/acme/jobs/4455667#app",
                "https://acme.example/careers?gh_jid=4455667"):
        m = c.get("/api/companion/applications", params={"url": url}, headers=h).json()["match"]
        assert m and m["id"] == capi["app"] and m["company"] == "Acme", url
    for url in ("https://boards.greenhouse.io/other/jobs/1", "https://evil.example/acme/jobs/4455667", "not a url", ""):
        assert c.get("/api/companion/applications", params={"url": url}, headers=h).json()["match"] is None, url
    apps_ = c.get("/api/companion/applications", headers=h).json()["applications"]
    assert [a["id"] for a in apps_] == [capi["app"]]                           # never another user's applications


def test_api_plan_fills_confirmed_facts_and_flags_the_rest(capi):
    r = capi["c"].post("/api/companion/plan", headers=capi["hb"],
                       json={"app_id": capi["app"], "url": "https://x", "fields": FORM})
    assert r.status_code == 200, r.text
    d = {f["key"]: f for f in r.json()["fields"]}
    assert (d["f1"]["action"], d["f1"]["value"]) == ("fill", "Bea")
    assert (d["f2"]["action"], d["f2"]["value"]) == ("fill", "Example")
    assert d["f3"]["value"] == "bea@example.com" and d["f4"]["action"] == "fill"
    assert d["f5"]["value"] == "https://www.linkedin.com/in/bea-example"
    assert (d["f6"]["action"], d["f6"]["value"]) == ("choose", "No")             # has an Irish work permit
    assert (d["f7"]["action"], d["f7"]["value"]) == ("choose", "Decline to self-identify")
    assert d["f8"]["value"] == "I like building payment systems."               # from the answer library
    assert d["f9"]["action"] == "unknown"
    assert d["f10"]["action"] == "upload_resume"
    assert d["f11"]["action"] == "needs_consent"                                 # privacy boxes left for the user
    assert d["f12"]["action"] == "skip" and d["f12"]["value"] is None            # password never filled
    assert d["f13"]["action"] == "skip" and d["f13"]["value"] is None            # SSN never filled
    assert d["f14"]["value"] == "1 month"
    s = r.json()["summary"]
    assert s["total"] == len(FORM) and s["needs_you"] >= 3
    assert r.json()["job"] == {"title": "Backend Engineer", "company": "Acme"}
    # with the privacy permission switched on, required consent boxes are ticked
    dd = capi["w"].db()
    pol = settings_mod.get(dd, "auto_apply", capi["bea"])
    settings_mod.set_value(dd, "auto_apply", {**pol, "accept_privacy_notices": True}, "x", user_id=capi["bea"])
    dd.close()
    d = {f["key"]: f for f in capi["c"].post("/api/companion/plan", headers=capi["hb"],
                                             json={"fields": FORM}).json()["fields"]}
    assert d["f11"]["action"] == "check"
    # the audit records counts and host only, never the answers
    dd = capi["w"].db()
    dets = [r["detail_json"] for r in dd.all("SELECT detail_json FROM audit_events WHERE action='companion_fill'")]
    dd.close()
    assert len(dets) == 2 and '"host": "x"' in dets[0]
    assert not any("bea@example.com" in d_ or "payment" in d_ or "Example" in d_ for d_ in dets)


def test_api_plan_rejects_bad_input_and_foreign_ids(capi):
    c, h = capi["c"], capi["hb"]
    assert c.post("/api/companion/plan", headers=h, content=b"{not json").status_code == 400
    assert c.post("/api/companion/plan", headers=h, json=[1, 2]).status_code == 400
    assert c.post("/api/companion/plan", headers=h, json={"fields": "nope"}).status_code == 400
    assert c.post("/api/companion/plan", headers=h, json={"fields": [], "app_id": "abc"}).status_code == 400
    assert c.post("/api/companion/plan", headers=h, json={"fields": [], "app_id": -3}).status_code == 400
    assert c.post("/api/companion/plan", headers=h, content=b'{"fields": "' + b"x" * 600_000 + b'"}').status_code == 413
    # another user's application id is simply not found
    r = c.post("/api/companion/plan", headers=h, json={"fields": FORM, "app_id": capi["cara_app"]})
    assert r.status_code == 404
    # garbage entries, unknown kinds and huge forms are tolerated (truncated to MAX_FIELDS)
    junk = [1, None, "x", {"key": "k", "label": "Email", "kind": "hologram"}] + \
        [{"key": f"k{i}", "label": f"Q{i}", "kind": "text"} for i in range(400)]
    r = c.post("/api/companion/plan", headers=h, json={"fields": junk})
    assert r.status_code == 200
    out = r.json()["fields"]
    assert len(out) == companion.MAX_FIELDS - 3 and out[0]["value"] == "bea@example.com"
    assert c.post("/api/companion/plan", headers=h, json={}).json()["summary"]["total"] == 0


def test_api_resume_download(capi):
    c = capi["c"]
    r = c.get("/api/companion/resume", params={"app_id": capi["app"]}, headers=capi["hb"])
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    assert 'filename="Bea_Example_Resume.pdf"' in r.headers["content-disposition"]
    assert c.get("/api/companion/resume", headers=capi["hb"]).content.startswith(b"%PDF")
    assert c.get("/api/companion/resume", params={"app_id": capi["cara_app"]}, headers=capi["hb"]).status_code == 404
    r = c.get("/api/companion/resume", headers=capi["hc"])                     # cara has no resume
    assert r.status_code == 404 and "Upload your resume" in r.json()["error"]
    assert c.get("/api/companion/resume", params={"app_id": "x"}, headers=capi["hb"]).status_code == 400


def test_api_learn_saves_only_reusable_answers(capi):
    c, h, d = capi["c"], capi["hb"], capi["w"].db()
    jid = insert_job(d, title="Data Eng", company="Initech", user_id=capi["bea"], app_status="awaiting_approval")
    held = d.scalar("SELECT id FROM applications WHERE job_id=?", (jid,))
    audit(d, actor="autoapply", entity="application", entity_id=held, action="autoapply_handback",
          detail={"code": "unknown_questions", "unknown_questions": ["What is your favourite colour?"]})
    d.close()
    items = [{"question": "What is your favourite colour?", "answer": "Blue"},
             {"question": "Email address", "answer": "bea@example.com"},              # personal: from Settings
             {"question": "Phone", "answer": "123"},
             {"question": "Gender", "answer": "Female"},                               # EEO: never stored
             {"question": "Bank account number", "answer": "123"},                    # sensitive
             {"question": "", "answer": "x"}, {"question": "Q?", "answer": ""}, "junk",
             {"question": "Do you have a driving licence?", "answer": "Yes"}]
    r = c.post("/api/companion/learn", headers=h, json={"items": items})
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 2 and r.json()["skipped"] == 6 and r.json()["requeued"] == 1
    d = capi["w"].db()
    lib = {x["question"]: x["answer"] for x in settings_mod.get(d, "answer_library", capi["bea"])}
    d.close()
    assert lib["What is your favourite colour?"] == "Blue" and lib["Do you have a driving licence?"] == "Yes"
    assert "Gender" not in lib and "Email address" not in lib
    assert c.post("/api/companion/learn", headers=h, json={"items": "x"}).status_code == 400
    assert c.post("/api/companion/learn", headers=h, json={"items": []}).json()["saved"] == 0
    # the next plan uses the learned answer
    d2 = {f["key"]: f for f in c.post("/api/companion/plan", headers=h, json={"fields": FORM}).json()["fields"]}
    assert d2["f9"]["value"] == "Blue"


def test_api_mark_applied(capi):
    c, h = capi["c"], capi["hb"]
    assert c.post("/api/companion/applied", headers=h, json={"url": "https://x"}).status_code == 400
    assert c.post("/api/companion/applied", headers=h, json={"app_id": capi["cara_app"]}).status_code == 404
    r = c.post("/api/companion/applied", headers=h, json={
        "app_id": capi["app"], "url": "https://boards.greenhouse.io/acme/jobs/4455667/confirmation",
        "confirmation_text": "Thank you for applying to Acme!"})
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    d = capi["w"].db()
    a = d.one("SELECT status, external, submission_evidence_json FROM applications WHERE id=?", (capi["app"],))
    d.close()
    assert a["status"] == "submitted" and "Application Companion" in a["submission_evidence_json"]
    r = c.post("/api/companion/applied", headers=h, json={"app_id": capi["app"]})
    assert r.status_code == 409 and "already" in r.json()["error"]


def test_api_mark_applied_after_approval_uses_outcome_flow(capi):
    d = capi["w"].db()
    with d.transaction():
        d.run("UPDATE applications SET status='application_started' WHERE id=?", (capi["app"],))
    d.close()
    r = capi["c"].post("/api/companion/applied", headers=capi["hb"], json={
        "app_id": capi["app"], "url": "https://x", "confirmation_text": ""})
    assert r.status_code == 200 and r.json()["status"] == "submitted"


def test_api_ai_draft_needs_a_grant(capi, monkeypatch):
    c, h = capi["c"], capi["hb"]
    r = c.post("/api/companion/draft", headers=h, json={"question": "Why do you want this job?"})
    assert r.status_code == 403 and "not been granted" in r.json()["error"]
    import jobagent.web.app as webapp
    prov = FakeAI()
    monkeypatch.setattr(webapp, "make_provider", lambda cfg: prov)
    d = capi["w"].db()
    users_mod.set_ai_access(d, capi["bea"], True, "x")
    d.close()
    assert c.get("/api/companion/me", headers=h).json()["ai"]["allowed"] is True
    r = c.post("/api/companion/draft", headers=h, json={"question": "Why do you want this job?", "app_id": capi["app"]})
    assert r.status_code == 200 and r.json()["answer"].startswith("I build Python")
    assert c.post("/api/companion/draft", headers=h, json={"question": "Hi"}).status_code == 400
    prov.text = '{"answer": ""}'
    r = c.post("/api/companion/draft", headers=h, json={"question": "Describe your PhD research in detail"})
    assert r.status_code == 422
    prov.text = "not json at all"
    r = c.post("/api/companion/draft", headers=h, json={"question": "Another different question here"})
    assert r.status_code == 502
    # revoking stops drafts at once
    d = capi["w"].db()
    users_mod.set_ai_access(d, capi["bea"], False, "x")
    d.close()
    assert c.post("/api/companion/draft", headers=h, json={"question": "Why do you want this job?"}).status_code == 403


# =============================================================================== COMPANION: real browser

FORM_HTML = """<!doctype html><html><body>
<form id="app" onsubmit="window.submitted = true; return false;">
  <label for="fn">First Name *</label><input id="fn" name="first_name" required>
  <label for="ln">Last Name *</label><input id="ln" name="last_name" required>
  <label for="em">Email *</label><input id="em" type="email" name="email" required>
  <label for="ph">Phone</label><input id="ph" type="tel" name="phone">
  <label for="li">LinkedIn Profile</label><input id="li" name="linkedin" value="">
  <label for="sp">Will you now or in the future require visa sponsorship to work in Ireland? *</label>
  <select id="sp" name="sponsor" required><option value="">Select...</option><option value="y">Yes</option><option value="n">No</option></select>
  <label for="ge">Gender</label>
  <select id="ge" name="gender"><option value="">Select...</option><option>Male</option><option>Female</option><option value="d">Decline to self-identify</option></select>
  <fieldset><legend>Are you willing to relocate? *</legend>
    <label><input type="radio" name="reloc" value="yes" required> Yes</label>
    <label><input type="radio" name="reloc" value="no"> No</label></fieldset>
  <label for="why">Why do you want to work at Acme? *</label><textarea id="why" name="why" required></textarea>
  <label for="col">What is your favourite colour? *</label><input id="col" name="colour" required>
  <label for="cv">Resume/CV *</label><input id="cv" type="file" name="resume" required>
  <label><input type="checkbox" id="pp" name="privacy" required> I have read and agree to the privacy policy *</label>
  <label for="pw">Password *</label><input id="pw" type="password" name="pw" required>
  <label for="pre">Current location</label><input id="pre" name="loc" value="Already typed by me">
  <button type="submit" id="go">Submit application</button>
</form>
<script>
  // React-style controlled input: state only updates on input events
  window.state = {};
  document.getElementById('fn').addEventListener('input', e => { window.state.fn = e.target.value; });
  document.getElementById('cv').addEventListener('change', e => { window.state.cv = e.target.files[0] && e.target.files[0].name; });
</script>
</body></html>"""


@pytest.fixture(scope="module")
def playwright_rt():
    """One Playwright runtime per module (the sync API cannot run two in the same thread)."""
    pw_mod = pytest.importorskip("playwright.sync_api")
    p = pw_mod.sync_playwright().start()
    yield p
    p.stop()


@pytest.fixture(scope="module")
def chromium(playwright_rt):
    try:
        browser = playwright_rt.chromium.launch()
    except Exception as e:                                            # pragma: no cover
        pytest.skip(f"Chromium not available: {e}")
    yield browser
    browser.close()


def _inject(page):
    files = companion.extension_files("http://localhost")
    page.add_script_tag(content=files["extract.js"].decode())
    page.add_script_tag(content=files["fill.js"].decode())


def test_extension_fills_a_real_form_in_chromium(capi, chromium):
    page = chromium.new_page()
    page.set_content(FORM_HTML)
    _inject(page)
    scan = page.evaluate("() => JACompanion.scan()")
    labels = {f["label"] for f in scan["fields"]}
    assert "What is your favourite colour? *" in labels and any("Resume" in lbl for lbl in labels)
    resume = next(f for f in scan["fields"] if f["kind"] == "file")
    assert resume["hint"] == "resume"
    plan = capi["c"].post("/api/companion/plan", headers=capi["hb"],
                          json={"app_id": capi["app"], "fields": scan["fields"]}).json()
    pdf = capi["c"].get("/api/companion/resume", params={"app_id": capi["app"]}, headers=capi["hb"])
    files = {"resume": {"data": base64.b64encode(pdf.content).decode(), "name": "Bea_Example_Resume.pdf",
                        "type": "application/pdf"}}
    rep = page.evaluate("([f, d, x]) => JACompanion.apply(f, d, x)", [scan["fields"], plan["fields"], files])
    v = lambda sel: page.eval_on_selector(sel, "e => e.value")          # noqa: E731
    assert v("#fn") == "Bea" and v("#ln") == "Example" and v("#em") == "bea@example.com"
    assert v("#li") == "https://www.linkedin.com/in/bea-example"
    assert v("#sp") == "n"                                               # "No": Irish work permit
    assert v("#ge") == "d"                                               # decline to self-identify
    assert page.is_checked("input[name=reloc][value=yes]")
    assert v("#why") == "I like building payment systems."
    assert v("#col") == "" and v("#pw") == ""                            # unknown + password untouched
    assert not page.is_checked("#pp")                                    # consent left for the user
    assert v("#pre") == "Already typed by me"                            # never overwrites the user's own text
    assert page.evaluate("window.state") == {"fn": "Bea", "cv": "Bea_Example_Resume.pdf"}   # framework saw it
    assert page.evaluate("document.getElementById('cv').files.length") == 1
    assert page.evaluate("!!window.submitted") is False                  # never submits
    needs = {n["question"] for n in rep["needs"]}
    assert any("colour" in q for q in needs) and any("privacy" in q for q in needs)
    assert not any("location" in q.lower() for q in needs) and rep["kept"] == 1     # own answer kept, not flagged
    assert page.eval_on_selector("#pre", "e => e.dataset.jaMark || ''") == ""
    assert rep["filled"] >= 9
    assert page.eval_on_selector("#fn", "e => e.dataset.jaMark") == "filled"
    assert page.eval_on_selector("#col", "e => e.dataset.jaMark") == "needs"

    # the user answers the rest; "Save my answers" offers only what they typed themselves
    page.fill("#col", "Blue")
    got = page.evaluate("() => JACompanion.collect()")
    qs = {g["question"]: g["answer"] for g in got}
    assert qs.get("What is your favourite colour?") == "Blue"
    assert "First Name" not in qs and not any("Password" in q for q in qs)
    r = capi["c"].post("/api/companion/learn", headers=capi["hb"], json={"items": got})
    assert r.json()["saved"] >= 1

    # second visit: everything known fills, the learned answer included
    page.set_content(FORM_HTML)
    _inject(page)
    scan = page.evaluate("() => JACompanion.scan()")
    plan = capi["c"].post("/api/companion/plan", headers=capi["hb"], json={"fields": scan["fields"]}).json()
    page.evaluate("([f, d]) => JACompanion.apply(f, d, {})", [scan["fields"], plan["fields"]])
    assert v("#col") == "Blue"
    # open questions + AI draft placement only into empty textareas
    assert page.evaluate("() => JACompanion.openQuestions()") == []
    page.evaluate("() => { document.getElementById('why').value = ''; }")
    oq = page.evaluate("() => JACompanion.openQuestions()")
    assert len(oq) == 1 and page.evaluate("([k]) => JACompanion.putDraft(k, 'Draft text')", [oq[0]["key"]])
    assert v("#why") == "Draft text" and page.eval_on_selector("#why", "e => e.dataset.jaMark") == "draft"
    assert page.evaluate("([k]) => JACompanion.putDraft(k, 'Other')", [oq[0]["key"]]) is False
    page.evaluate("() => JACompanion.clearMarks()")
    assert page.eval_on_selector("#fn", "e => e.dataset.jaMark || ''") == ""
    # confirmation detection
    page.set_content("<h1>Thank you for applying to Acme! We will be in touch.</h1>")
    _inject(page)
    assert page.evaluate("() => JACompanion.confirmation()").startswith("Thank you for applying")
    page.set_content("<h1>Careers</h1>")
    _inject(page)
    assert page.evaluate("() => JACompanion.confirmation()") == ""
    assert page.evaluate("() => JACompanion.scan()")["fields"] == []
    page.close()


def test_extension_handles_custom_dropdowns_and_button_groups(capi, chromium):
    page = chromium.new_page()
    page.set_content("""<!doctype html><html><body>
      <div><label id="cl">Country *</label>
        <input role="combobox" aria-labelledby="cl" id="cb" aria-required="true" autocomplete="off">
        <ul id="lb" role="listbox"></ul></div>
      <div><div class="label">Are you legally authorized to work in Ireland? *</div>
        <div class="btns"><button type="button" id="by">Yes</button><button type="button" id="bn">No</button></div>
        <input type="hidden" name="auth"></div>
      <script>
        const cb = document.getElementById('cb'), lb = document.getElementById('lb');
        cb.addEventListener('input', () => {
          lb.innerHTML = '';
          ['Ireland', 'Iceland', 'India'].filter(c => c.toLowerCase().startsWith(cb.value.toLowerCase().slice(0, 2)))
            .forEach(c => { const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = c;
              li.onclick = () => { cb.value = c; cb.dataset.chosen = c; lb.innerHTML = ''; }; lb.appendChild(li); });
        });
        document.querySelectorAll('.btns button').forEach(b => b.onclick = () => { window.clicked = b.textContent; });
      </script></body></html>""")
    _inject(page)
    scan = page.evaluate("() => JACompanion.scan()")
    kinds = {f["label"]: f["kind"] for f in scan["fields"]}
    assert kinds.get("Country *") == "combobox" and "buttons" in kinds.values()
    plan = capi["c"].post("/api/companion/plan", headers=capi["hb"], json={"fields": scan["fields"]}).json()
    page.evaluate("([f, d]) => JACompanion.apply(f, d, {})", [scan["fields"], plan["fields"]])
    assert page.eval_on_selector("#cb", "e => e.dataset.chosen") == "Ireland"
    assert page.evaluate("window.clicked") == "Yes"
    page.close()


def test_installed_extension_end_to_end(capi, tmp_path, playwright_rt):
    """The real extension (service worker, options page, popup) loaded into Chromium, talking to a live JobAgent
    server: connect with a token, fill a form on an https page, learn the user's own answer."""
    import socket
    import threading
    import time

    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(capi["w"].client.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    base = f"http://127.0.0.1:{port}"
    ext = tmp_path / "ext"
    ext.mkdir()
    for name, data in companion.extension_files(base).items():
        (ext / name).write_bytes(data)
    token = capi["hb"]["Authorization"].split()[1]
    p = playwright_rt
    if True:
        try:
            ctx = p.chromium.launch_persistent_context(
                str(tmp_path / "profile"), headless=True, channel="chromium",
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        except Exception as e:                                        # pragma: no cover
            pytest.skip(f"Chromium with extensions not available: {e}")
        try:
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
            ext_id = sw.url.split("/")[2]
            # 1. options page: server pre-filled from defaults.js; a bad token is refused; the right one connects
            opt = ctx.new_page()
            opt.goto(f"chrome-extension://{ext_id}/options.html")
            assert opt.input_value("#server") == base
            opt.fill("#token", "not-a-token")
            opt.click("#save")
            opt.wait_for_selector("#status:not(:empty)")
            assert "does not look like" in opt.text_content("#status")
            opt.fill("#token", "jac_" + "Z" * 43)
            opt.click("#save")
            opt.wait_for_selector("#status:has-text('token')")
            opt.fill("#token", token)
            opt.click("#save")
            opt.wait_for_selector("#status:has-text('Connected')")
            assert opt.text_content("#status") == "Connected as Bea."
            # 2. an application form on an https employer site
            ctx.route("https://boards.greenhouse.io/**", lambda route: route.fulfill(
                status=200, content_type="text/html", body=FORM_HTML))
            form = ctx.new_page()
            form.goto("https://boards.greenhouse.io/acme/jobs/4455667")
            tab_id = sw.evaluate("async () => (await chrome.tabs.query({url: 'https://boards.greenhouse.io/*'}))[0].id")
            pop = ctx.new_page()
            pop.goto(f"chrome-extension://{ext_id}/popup.html?tab={tab_id}")
            pop.wait_for_selector("#who:has-text('Signed in')")
            assert pop.input_value("#app") == str(capi["app"])                 # matched by URL
            assert pop.is_hidden("#draft")                                     # no AI grant
            pop.click("#fill")
            pop.wait_for_selector("#report:has-text('Filled')", timeout=20000)
            assert form.input_value("#fn") == "Bea" and form.input_value("#em") == "bea@example.com"
            assert form.input_value("#sp") == "n" and form.input_value("#why") == "I like building payment systems."
            assert form.evaluate("document.getElementById('cv').files[0].name") == "Bea_Example_Resume.pdf"
            assert form.evaluate("document.getElementById('cv').files[0].size") > 500
            assert "colour" in pop.text_content("#report") and "Kept 1 answer" in pop.text_content("#report")
            assert "Current location" not in pop.text_content("#report")
            assert form.evaluate("!!window.submitted") is False
            import os
            if os.environ.get("JA_SHOTS"):                              # optional screenshots for docs/review
                pop.set_viewport_size({"width": 380, "height": 640})
                pop.screenshot(path=os.path.join(os.environ["JA_SHOTS"], "companion_popup.png"), full_page=True)
                form.set_viewport_size({"width": 900, "height": 1100})
                form.screenshot(path=os.path.join(os.environ["JA_SHOTS"], "companion_form.png"), full_page=True)
            # 3. learn the answer the user typed
            form.fill("#col", "Teal")
            pop.click("#learn")
            pop.wait_for_selector("#learn-list li")
            assert "Teal" in pop.text_content("#learn-list")
            pop.click("#learn-save")
            pop.wait_for_selector("#report:has-text('Saved')")
            d = capi["w"].db()
            lib = {x["question"]: x["answer"] for x in settings_mod.get(d, "answer_library", capi["bea"])}
            d.close()
            assert lib.get("What is your favourite colour?") == "Teal"
            # 4. record the submission
            pop.click("#applied")
            pop.wait_for_selector("#report:has-text('Recorded')")
            d = capi["w"].db()
            assert d.scalar("SELECT status FROM applications WHERE id=?", (capi["app"],)) == "submitted"
            d.close()
            # 5. web pages can never use the extension's token (the API bridge rejects content scripts)
            res = form.evaluate("""() => new Promise(r => { try { chrome.runtime.sendMessage('x', {}, () => r('sent')); }
                                    catch (e) { r('blocked'); } })""")
            assert res == "blocked"
            # 6. a form embedded in a cross-origin iframe on the company's careers page
            ctx.route("https://careers.acme.example/**", lambda route: route.fulfill(
                status=200, content_type="text/html",
                body='<h1>Acme careers</h1><iframe id="gh" width="900" height="900" '
                     'src="https://boards.greenhouse.io/embed/job_app?for=acme&token=4455667"></iframe>'))
            company = ctx.new_page()
            company.goto("https://careers.acme.example/jobs?gh_jid=4455667")
            company.frame_locator("#gh").locator("#fn").wait_for()
            tab2 = sw.evaluate("async () => (await chrome.tabs.query({url: 'https://careers.acme.example/*'}))[0].id")
            pop2 = ctx.new_page()
            pop2.goto(f"chrome-extension://{ext_id}/popup.html?tab={tab2}")
            pop2.wait_for_selector("#who:has-text('Signed in')")
            pop2.click("#fill")
            pop2.wait_for_selector("#report:has-text('Filled')", timeout=20000)
            inner = company.frame_locator("#gh")
            assert inner.locator("#fn").input_value() == "Bea" and inner.locator("#em").input_value() == "bea@example.com"
            assert inner.locator("#cv").evaluate("e => e.files.length") == 1
        finally:
            ctx.close()
            server.should_exit = True


def test_screening_answers_points_manual_cases_to_the_companion(tmp_path):
    w = web(tmp_path)
    assert "need a person on the employer" not in w.client.get("/answers").text
    d = w.db()
    for code in ("unsupported_ats", "captcha", "unknown_questions"):
        jid = insert_job(d, app_status="awaiting_approval")
        aid = d.scalar("SELECT id FROM applications WHERE job_id=?", (jid,))
        audit(d, actor="autoapply", entity="application", entity_id=aid, action="autoapply_handback",
              detail={"code": code, "unknown_questions": ["Do you hold a driving licence?"]
                      if code == "unknown_questions" else []})
    d.close()
    page = w.client.get("/answers").text
    assert "2 applications need a person on the employer" in page and 'href="/companion"' in page
