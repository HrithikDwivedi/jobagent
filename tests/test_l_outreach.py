"""L. Recruiter outreach + screening-answer library (SYNTHETIC data)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from conftest import extract_csrf
from factories import make_user
from webdata import insert_job

from jobagent import answers as answers_mod
from jobagent import outreach
from jobagent import settings as settings_mod
from jobagent.autoapply.matcher import Field, Matcher
from jobagent.db import audit, iso


# ----------------------------------------------------------------------------- contacts from job posts

def test_extract_contacts_filters_and_attributes_names():
    text = ("Questions? Contact: Priya Sharma (priya.sharma@pwc.example). Recruiter - John Smith, john@x.example. "
            "Our careers team at careers@acme.example will reply. Privacy: privacy@acme.example, "
            "no-reply@acme.example, accommodations@acme.example. Logo: logo@2x.png")
    got = outreach.extract_contacts(text)
    assert [c["email"] for c in got] == ["priya.sharma@pwc.example", "john@x.example", "careers@acme.example"]
    assert got[0]["name"] == "Priya Sharma" and got[1]["name"] == "John Smith"
    assert got[2]["name"] is None and got[2]["kind"] == "generic"


def test_draft_uses_only_known_facts():
    job = {"title": "Backend Engineer", "employer": "Acme", "location_raw": "Dublin, Ireland",
           "details_json": '{"must_have": [{"skill": "Python", "credit": 1.0}, {"skill": "Go", "credit": 0.2}]}'}
    subject, body = outreach.draft(job, {"full_name": "Alex Doe", "email": "alex@example.com"}, "Priya Sharma",
                                   "Software Engineer")
    assert subject == "Backend Engineer at Acme – Alex Doe"
    assert body.startswith("Hi Priya,") and "Python" in body and "Go" not in body
    assert "Software Engineer" in body and body.rstrip().endswith("alex@example.com")


# ----------------------------------------------------------------------------- web: recruiters

def _job(d, **kw):
    jid = insert_job(d, title="Platform Engineer", company="Globex", score=82,
                     description="Kubernetes and Go. Recruiter: Dana Lee, dana.lee@globex.example", **kw)
    d.run("UPDATE jobs SET is_sample=0")
    return jid


def test_recruiters_board_compose_and_gmail_handoff(client):
    d = client.db()
    jid = _job(d)
    insert_job(d, title="Unrelated", company="Initech", score=20, description="mail hr@initech.example")
    r = client.client.get("/recruiters")
    assert r.status_code == 200
    assert "dana.lee@globex.example" in r.text and "From the job post" in r.text
    assert "hr@initech.example" not in r.text                       # below the default match threshold
    r = client.client.get("/recruiters/compose", params={"job": jid, "to": "dana.lee@globex.example", "name": "Dana Lee"})
    assert r.status_code == 200 and "Hi Dana," in r.text
    csrf = extract_csrf(r.text)
    form = {"csrf": csrf, "job_id": jid, "to": "dana.lee@globex.example", "to_name": "Dana Lee",
            "subject": "Platform Engineer at Globex", "body": "Hi Dana,\nShort note.", "channel": "gmail"}
    r = client.client.post("/recruiters/send", data=form, follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.netloc == "mail.google.com"
    q = parse_qs(loc.query)
    assert q["to"] == ["dana.lee@globex.example"] and q["su"] == ["Platform Engineer at Globex"]
    assert q["body"] == ["Hi Dana,\nShort note."]
    row = d.one("SELECT * FROM outreach_messages")
    assert row["channel"] == "gmail" and row["status"] == "opened" and row["job_id"] == jid
    # a second message to the same person about the same job needs an explicit confirmation
    r = client.client.post("/recruiters/send", data=dict(form, channel="mail_app"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/recruiters/compose")
    r = client.client.post("/recruiters/send", data=dict(form, channel="mail_app", confirm_repeat="1"),
                           follow_redirects=False)
    assert r.status_code == 200 and 'href="mailto:dana.lee@globex.example?subject=' in r.text
    assert d.scalar("SELECT COUNT(*) FROM outreach_messages") == 2
    assert "Contacted" in client.client.get("/recruiters").text


def test_recruiters_send_rejects_bad_input_and_server_without_provider(client):
    d = client.db()
    jid = _job(d)
    csrf = client.csrf("/recruiters")
    base = {"csrf": csrf, "job_id": jid, "to": "dana.lee@globex.example", "subject": "s", "body": "b"}
    r = client.client.post("/recruiters/send", data=dict(base, to="not-an-email", channel="gmail"),
                           follow_redirects=False)
    assert r.headers["location"].startswith("/recruiters/compose")
    r = client.client.post("/recruiters/send", data=dict(base, channel="server"), follow_redirects=False)
    assert r.headers["location"].startswith("/recruiters/compose")      # console provider: no server sending
    assert d.scalar("SELECT COUNT(*) FROM outreach_messages") == 0
    r = client.client.post("/recruiters/send", data=dict(base, channel="gmail"), follow_redirects=False)
    assert "csrf" not in r.headers["location"] and r.status_code == 303
    r = client.client.post("/recruiters/send", data=dict(base, csrf="bad", channel="gmail"), follow_redirects=False)
    assert r.status_code in (400, 403)


def test_manual_contacts_are_private_per_user(client, tmp_path):
    d = client.db()
    jid = _job(d, )
    csrf = client.csrf("/recruiters")
    r = client.client.post("/recruiters/contacts", data={"csrf": csrf, "job_id": jid, "company": "Globex",
                                                        "name": "Sam Park", "email": "Sam.Park@Globex.example",
                                                        "profile_url": "https://www.linkedin.com/in/sam"},
                           follow_redirects=False)
    assert r.status_code == 303
    assert "sam.park@globex.example" in client.client.get("/recruiters").text
    dup = client.client.post("/recruiters/contacts", data={"csrf": csrf, "job_id": jid, "company": "Globex",
                                                          "email": "sam.park@globex.example"}, follow_redirects=False)
    assert d.scalar("SELECT COUNT(*) FROM recruiter_contacts") == 1 and dup.status_code == 303
    bad = client.client.post("/recruiters/contacts", data={"csrf": csrf, "company": "Globex", "email": "x@y.example",
                                                          "profile_url": "javascript:alert(1)"}, follow_redirects=False)
    assert bad.status_code == 303 and d.scalar("SELECT COUNT(*) FROM recruiter_contacts") == 1
    cid = d.scalar("SELECT id FROM recruiter_contacts")
    # another user's contact id cannot be deleted
    other = make_user(d, "zed")
    d.run("UPDATE recruiter_contacts SET user_id=? WHERE id=?", (other, cid))
    client.client.post(f"/recruiters/contacts/{cid}/delete", data={"csrf": csrf}, follow_redirects=False)
    assert d.scalar("SELECT COUNT(*) FROM recruiter_contacts") == 1
    assert "sam.park@globex.example" not in client.client.get("/recruiters").text


# ----------------------------------------------------------------------------- screening answers

def test_matcher_uses_answer_library_for_required_questions():
    f = Field(key="q1", label="Have you worked with Kubernetes in production?*", kind="text", required=True)
    m = Matcher({"full_name": "Alex Doe"}, {"country": "IE"},
                [{"question": "Have you worked with Kubernetes in production?", "answer": "Yes, for 2 years"}])
    d = m.decide(f)
    assert d.action == "fill" and d.value == "Yes, for 2 years"
    assert Matcher({"full_name": "Alex Doe"}, {"country": "IE"}, []).decide(f).blocking


def test_answers_page_lists_pending_saves_and_requeues(client):
    d = client.db()
    jid = _job(d)
    d.run("INSERT INTO applications(job_id, user_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
          (jid, 1, "awaiting_approval", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    app_id = d.scalar("SELECT id FROM applications")
    audit(d, actor="autoapply", entity="application", entity_id=app_id, action="autoapply_handback",
          detail={"code": "unknown_questions", "unknown_questions": ["What is your notice period?",
                                                                    "I agree to the privacy policy"]})
    r = client.client.get("/answers")
    assert r.status_code == 200
    assert "What is your notice period?" in r.text and "Required questions without a saved answer" in r.text
    csrf = extract_csrf(r.text)
    r = client.client.post("/answers", data={"csrf": csrf, "question": ["What is your notice period?",
                                                                        "I agree to the privacy policy"],
                                             "answer": ["Immediately", ""]}, follow_redirects=False)
    assert r.status_code == 303
    lib = settings_mod.get(d, "answer_library", 1)
    assert lib == [{"question": "What is your notice period?", "answer": "Immediately"}]
    assert d.scalar("SELECT updated_at FROM applications WHERE id=?", (app_id,)) > "2026-01-01T00:00:00+00:00"
    page = client.client.get("/answers").text
    assert "What is your notice period?" in page                     # now in the saved list
    assert [p["question"] for p in answers_mod.overview(d, user_id=1)["pending"]] == ["I agree to the privacy policy"]
    # consent switch
    client.client.post("/answers/consent", data={"csrf": csrf, "accept": "1"}, follow_redirects=False)
    assert settings_mod.get_all(d, 1)["auto_apply"]["accept_privacy_notices"] is True
    # delete
    client.client.post("/answers/delete", data={"csrf": csrf, "question": "What is your notice period?"},
                       follow_redirects=False)
    assert settings_mod.get(d, "answer_library", 1) == []


def test_answer_library_validation():
    v = settings_mod.validate("answer_library", [{"question": "  Notice   period? ", "answer": " 1 month "},
                                                 {"question": "notice period?", "answer": "dup"},
                                                 {"question": "", "answer": "x"}])
    assert v == [{"question": "Notice period?", "answer": "1 month"}]
    assert iso()  # keep import used
