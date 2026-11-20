"""O. Salary in the user's currency (saved once, converted per job/form), the simple "I've applied" flow, and the
administrator's control over which screens each user (or industry) sees, plus full AI access. SYNTHETIC data."""
from __future__ import annotations

import re

import httpx
import pytest

from conftest import TEST_PASSWORD, extract_csrf, web_factory
from factories import ADMIN, make_user
from jobagent import access, fx
from jobagent import applications as apps
from jobagent import settings as settings_mod
from jobagent.ai import ai_access
from jobagent.autoapply.matcher import Field, Matcher, salary_option
from jobagent.http import HttpClient
from webdata import insert_job


# =============================================================================== money

@pytest.mark.parametrize("text,value", [
    ("45000", 45000), ("45,000", 45000), ("15,00,000", 1500000), ("12 LPA", 1200000), ("1.2 lakh", 120000),
    ("2 crore", 20000000), ("35.5k", 35500), ("£42,000", 42000), ("€1.5m", 1500000), ("", None), ("lots", None),
    (None, None), (-5, None),
])
def test_parse_amount(text, value):
    assert fx.parse_amount(text) == value


def test_conversion_rounding_and_periods():
    es = {"amount": 1500000, "currency": "INR", "period": "year"}
    assert fx.expected_for_job(es, {"country": "GB"})["amount"] == 12000
    assert fx.expected_for_job(es, {"country": "IE"}, period="month") == {
        "amount": 1200, "currency": "EUR", "period": "month", "from_amount": 1500000, "from_currency": "INR",
        "rate_date": "2026-11-18"}
    assert fx.expected_for_job(es, {"salary_currency": "AED"})["currency"] == "AED"
    assert fx.expected_for_job(es, {"country": "IN"})["amount"] == 1500000            # same currency
    assert fx.expected_for_job(es, {"country": "ZZ"})["currency"] == "INR"            # unknown country
    assert fx.expected_for_job({"amount": 25, "currency": "GBP", "period": "hour"}, {"country": "GB"})["amount"] == 52000
    assert fx.expected_for_job(None, {}) is None and fx.expected_for_job({"amount": 0}, {}) is None
    assert fx.nice_round(41873) == 42000 and fx.nice_round(3489) == 3500 and fx.nice_round(187654) == 190000


def test_rates_refresh_cache_and_fallback(db):
    xml = ("<Cube time='2026-11-19'><Cube currency='USD' rate='1.10'/><Cube currency='GBP' rate='0.80'/>"
           "<Cube currency='INR' rate='100.0'/></Cube>")
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, text=xml)
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    r = fx.rates(db, http)
    assert r["date"] == "2026-11-19" and r["rates"]["GBP"] == 0.8 and r["rates"]["AED"] == round(1.1 * 3.6725, 4)
    assert fx.rates(db, http) == r and len(calls) == 1                     # cached for the day
    assert fx.rates(None, HttpClient(transport=httpx.MockTransport(lambda q: httpx.Response(500)), sleep=lambda s: None,
                                     max_retries=0)) == fx.SNAPSHOT           # never raises, falls back
    assert fx.convert(100, "GBP", "INR", r) == pytest.approx(12500)


@pytest.mark.parametrize("label,kind,options,expected", [
    ("Expected salary", "text", [], "£12,000 per year"),
    ("Desired salary (GBP)", "number", [], "12000"),
    ("Expected monthly salary in EUR", "text", [], "1,200"),
    ("Expected CTC (in lakhs)", "text", [], "15"),
    ("Salary expectations", "select", ["Under £10,000", "£10,000 - £15,000", "£15,000 - £25,000", "£25,000+"],
     "£10,000 - £15,000"),
    ("Expected hourly rate", "number", [], "6"),
])
def test_forms_get_salary_in_their_currency(label, kind, options, expected):
    cand = {"expected_salary": {"amount": 1500000, "currency": "INR", "period": "year"}}
    m = Matcher(cand, {"country": "GB"}, [{"question": "Salary expectation", "answer": "stale old text"}])
    d = m.decide(Field("k", label, kind, options=options))
    assert d.value == expected and d.source == "settings"


def test_salary_option_bands():
    assert salary_option(["40k-50k", "50k-60k", "60k+"], 55000) == "50k-60k"
    assert salary_option(["40k-50k", "60k+"], 99000) == "60k+"
    assert salary_option(["£30,000 – £40,000"], 35000) == "£30,000 – £40,000"
    assert salary_option(["30,000 - 40,000"], 99999) is None
    assert salary_option(["Prefer not to say"], 1) is None


def test_settings_validation_for_salary():
    assert settings_mod.validate("min_salary", "15,00,000") == 1500000
    assert settings_mod.validate("min_salary", "") == 0
    with pytest.raises(settings_mod.SettingsError):
        settings_mod.validate("min_salary", "a lot")
    c = settings_mod.validate("candidate", {"expected_salary": {"amount": "45k", "currency": "GBP", "period": "year"},
                                            "salary_expectation": "old"})
    assert c["expected_salary"] == {"amount": 45000, "currency": "GBP", "period": "year"}
    assert c["salary_expectation"] == "£45,000 per year"                  # always follows the structured value
    for bad in ({"amount": 5, "currency": "XYZ"}, {"amount": 5, "period": "fortnight"}, {"amount": 2e9}):
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.validate("candidate", {"expected_salary": bad})
    assert settings_mod.validate("candidate", {"expected_salary": {"amount": ""}})["expected_salary"] is None


def test_prepared_answers_and_scoring_use_conversion(db):
    from jobagent.matching import score_job
    ans, _ = apps.screening_answers({"expected_salary": {"amount": 1500000, "currency": "INR"}, "full_name": "A",
                                     "email": "a@x.example"}, {"country": "GB"})
    sal = next(a["answer"] for a in ans if a["question"] == "Salary expectation")
    assert sal.startswith("£12,000 per year") and "converted from ₹1,500,000" in sal
    s = dict(settings_mod.DEFAULT_SETTINGS, min_salary=1500000, currency="INR")
    prof = {"skills": {}, "experience_years": 1, "languages": {}, "work_authorization": {}}
    job = {"title": "Backend Engineer", "country": "GB", "salary_max": 30000, "salary_currency": "GBP",
           "description_text": ""}
    r = score_job(job, {"must_have": [], "nice_to_have": []}, {"classification": "unknown", "needs_sponsorship": False},
                  prof, s)
    assert r["components"]["compensation"] == 1.0                          # £30k > ₹15L once converted
    low = score_job({**job, "salary_max": 9000}, {"must_have": [], "nice_to_have": []},
                    {"classification": "unknown", "needs_sponsorship": False}, prof, s)
    assert low["components"]["compensation"] == 0.1 and any("exchange rate" in g for g in low["gaps"])


def test_settings_page_saves_salary(client):
    c = client.client
    tok = client.csrf()
    r = c.post("/settings", data={"csrf": tok, "section": "candidate", "full_name": "A Person", "email": "a@x.example",
                                  "expected_amount": "15,00,000", "expected_currency": "INR",
                                  "expected_period": "year"}, follow_redirects=True)
    assert "Settings saved" in r.text
    r = c.post("/settings", data={"csrf": tok, "section": "search", "tier1": "GB\nIE", "tier2": "", "tier3": "",
                                  "work_arrangements": ["remote"], "min_salary": "12,00,000", "currency": "INR",
                                  "sponsorship_policy": "any", "min_match_score": "55"}, follow_redirects=True)
    assert "Settings saved" in r.text
    d = client.db()
    s = settings_mod.get_all(d, ADMIN)
    d.close()
    assert s["candidate"]["expected_salary"]["amount"] == 1500000 and s["min_salary"] == 1200000
    page = c.get("/settings").text
    assert "£12,000/year in United Kingdom" in page and "€15,000/year in Ireland" in page
    assert 'max="1000000"' not in page                                      # the old limit is gone
    r = c.post("/settings", data={"csrf": tok, "section": "candidate", "expected_amount": "loads"},
               follow_redirects=True)
    assert "must be a number" in r.text


# =============================================================================== I've applied

@pytest.mark.parametrize("status", ["shortlisted", "awaiting_approval", "ready_to_apply", "application_started",
                                    "submission_uncertain", "submission_failed", "closed"])
def test_mark_applied_from_any_pre_submission_state(db, status):
    jid = insert_job(db, app_status=status)
    aid = db.scalar("SELECT id FROM applications WHERE job_id=?", (jid,))
    with db.transaction():
        db.run("INSERT INTO approvals(application_id, fingerprint, snapshot_json, approved_by, approved_at, expires_at) "
               "VALUES (?,?,?,?,?,?)", (aid, "fp", "{}", "user:alex", "2026-01-01T00:00:00+00:00",
                                        "2099-01-01T00:00:00+00:00"))
    a = apps.mark_applied(db, aid, "user:alex", user_id=ADMIN, reference_number="REF-1")
    assert a["status"] == "submitted" and a["reference_number"] == "REF-1" and a["submitted_at"]
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE application_id=? AND revoked_at IS NULL", (aid,)) == 0
    with pytest.raises(apps.WorkflowError, match="already"):
        apps.mark_applied(db, aid, "user:alex", user_id=ADMIN)


def test_mark_applied_is_private(db):
    bea = make_user(db, "bea")
    jid = insert_job(db, app_status="awaiting_approval")
    aid = db.scalar("SELECT id FROM applications WHERE job_id=?", (jid,))
    with pytest.raises(apps.WorkflowError):
        apps.mark_applied(db, aid, "user:bea", user_id=bea)


def test_application_page_is_three_simple_steps(client):
    d = client.db()
    jid = insert_job(d, title="Forward Deployed Engineer", app_status="awaiting_approval",
                     apply_url="https://www.arbeitnow.example/jobs/fde")
    aid = d.scalar("SELECT id FROM applications WHERE job_id=?", (jid,))
    d.close()
    page = client.client.get(f"/applications/{aid}").text
    steps = re.findall(r'<ol class="stepper stepper-3".*?</ol>', page, re.S)[0]
    assert [s.strip() for s in re.findall(r"<li[^>]*>\s*([^<—]+)", steps)] == ["Ready to apply", "Applied", "Heard back"]
    assert "Opened on employer site" not in page and "Submitted (with evidence)" not in page
    assert "I’ve applied" in page and 'data-apply-link' in page and "Let auto-apply submit it for me" in page
    tok = extract_csrf(page)
    r = client.client.post(f"/applications/{aid}/applied", data={"csrf": tok}, follow_redirects=True)
    assert "Marked as applied" in r.text
    page = client.client.get(f"/applications/{aid}").text
    assert "I’ve applied" not in page                                        # done: the panel is gone
    assert client.client.post(f"/applications/{aid}/applied", data={"csrf": tok}, follow_redirects=True).status_code == 200
    assert client.client.post("/applications/999/applied", data={"csrf": tok}).status_code == 404
    assert client.client.post(f"/applications/{aid}/applied", data={}).status_code == 403


# =============================================================================== screens

def _career(db, uid, industries, roles, sponsor=True):
    settings_mod.set_value(db, "career", {"industries": industries, "roles": roles}, "x", user_id=uid)
    cand = settings_mod.get(db, "candidate", uid)
    settings_mod.set_value(db, "candidate", {**cand, "requires_sponsorship_outside": sponsor}, "x", user_id=uid)


def test_access_layers(anydb):
    db = anydb
    nurse, dev, legacy = make_user(db, "nina"), make_user(db, "dev"), make_user(db, "leo")
    _career(db, nurse, ["healthcare"], ["nurse"], sponsor=False)
    _career(db, dev, ["technology", "healthcare"], ["software_engineer"])
    assert access.resolve(db, ADMIN) == {"hidden": [], "source": "admin"}
    # default for every user: all screens except Screening Answers, Agent Runs and the Audit Log
    assert access.resolve(db, nurse) == {"hidden": ["/answers", "/runs", "/audit"], "source": "automatic"}
    assert access.resolve(db, dev)["hidden"] == ["/answers", "/runs", "/audit"]
    assert access.resolve(db, legacy)["hidden"] == ["/answers", "/runs", "/audit"]
    access.set_segment(db, "healthcare", ["/answers", "/companion", "/studio"], "admin")   # visible list
    r = access.resolve(db, nurse)
    assert r["source"] == "industry"
    assert r["hidden"] == ["/mnc", "/recruiters", "/sponsorship", "/prep", "/runs", "/audit"]
    assert access.resolve(db, dev)["hidden"] == ["/runs", "/audit"]         # hidden only if off for ALL industries
    access.set_user(db, nurse, "custom", ["/runs"], "admin")
    assert access.resolve(db, nurse) == {"hidden": [p for p in access.SCREEN_PATHS if p != "/runs"], "source": "custom"}
    access.set_user(db, nurse, "auto", [], "admin")
    assert access.resolve(db, nurse)["source"] == "industry"
    with pytest.raises(access.AccessError):
        access.set_user(db, ADMIN, "custom", [], "x")
    with pytest.raises(access.AccessError):
        access.set_user(db, nurse, "sideways", [], "x")
    with pytest.raises(access.AccessError):
        access.set_segment(db, "astrology", [], "x")
    assert any(r["username"] == "nina" for r in access.matrix(db))


def test_screens_are_enforced_on_the_server(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    d = w.db()
    nina = make_user(d, "nina")
    _career(d, nina, ["healthcare"], ["nurse"], sponsor=False)
    access.set_user(d, nina, "custom", ["/answers", "/studio"], "admin")
    from jobagent import companion
    token, _ = companion.create_token(d, user_id=nina, name="t", actor="x")
    d.close()
    assert w.login("nina", TEST_PASSWORD).status_code == 303
    home = w.client.get("/").text
    for hidden in ("/mnc", "/recruiters", "/companion", "/sponsorship", "/prep", "/runs", "/audit"):
        assert f'href="{hidden}"' not in home, hidden
    assert 'href="/answers"' in home and 'href="/studio"' in home and 'href="/jobs"' in home
    r = w.client.get("/runs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "not enabled for your account" in w.client.get("/runs").text
    assert w.client.get("/runs/abc", follow_redirects=False).status_code == 303
    tok = extract_csrf(home)
    assert w.client.post("/runs/start", data={"csrf": tok}).status_code == 404
    assert w.client.post("/recruiters/contacts", data={"csrf": tok}).status_code == 404
    assert w.client.get("/answers").status_code == 200 and w.client.get("/studio").status_code == 200
    assert w.client.get("/jobs").status_code == 200                         # core screens always open
    r = w.client.get("/api/companion/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and "not enabled" in r.json()["error"]


def test_admin_controls_screens_and_ai(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    d = w.db()
    nina, bob, pend = make_user(d, "nina"), make_user(d, "bob"), make_user(d, "pend", status="pending")
    _career(d, nina, ["healthcare"], ["nurse"])
    d.close()
    assert w.login().status_code == 303
    page = w.client.get("/admin/users").text
    assert "Who sees what" in page and "Grant full AI to all" in page and "Full access (no limit)" in page
    assert "Screens for Healthcare &amp; Life Sciences users" in page
    tok = extract_csrf(page)
    r = w.client.post(f"/admin/users/{nina}/screens", data={"csrf": tok, "mode": "custom", "screen": ["/answers"]},
                      follow_redirects=True)
    assert "Screens updated" in r.text and "Hidden: Top MNCs, Recruiters" in r.text
    r = w.client.post("/admin/segments/healthcare/screens", data={"csrf": tok, "screen": ["/answers", "/studio"]},
                      follow_redirects=True)
    assert "Screens for Healthcare" in r.text and "saved" in r.text
    assert w.client.post("/admin/segments/nope/screens", data={"csrf": tok}, follow_redirects=True).status_code == 200
    assert w.client.post(f"/admin/users/{ADMIN}/screens", data={"csrf": tok, "mode": "custom"},
                         follow_redirects=True).status_code == 200
    # full AI access: no monthly limit
    r = w.client.post(f"/admin/users/{nina}/ai-access", data={"csrf": tok, "grant": "1", "ai_mode": "full",
                                                              "monthly_limit_usd": "3"}, follow_redirects=True)
    d = w.db()
    assert ai_access(d, nina) == {"allowed": True, "limit": None, "spent": 0.0}
    d.close()
    w.client.post(f"/admin/users/{nina}/ai-access", data={"csrf": tok, "grant": "1", "ai_mode": "limit",
                                                          "monthly_limit_usd": "3"})
    d = w.db()
    assert ai_access(d, nina)["limit"] == 3.0
    d.close()
    r = w.client.post("/admin/ai/bulk", data={"csrf": tok, "grant": "1"}, follow_redirects=True)
    assert "granted (full access, no monthly limit) to 2 accounts" in r.text
    d = w.db()
    assert ai_access(d, bob)["allowed"] and ai_access(d, nina)["limit"] is None
    assert not ai_access(d, pend)["allowed"]                                # pending accounts are skipped
    d.close()
    r = w.client.post("/admin/ai/bulk", data={"csrf": tok, "grant": "0"}, follow_redirects=True)
    assert "removed from 2 accounts" in r.text
    # non-admins can do none of it
    w.client.cookies.clear()
    assert w.login("bob", TEST_PASSWORD).status_code == 303
    t2 = w.csrf("/settings")
    for path, data in ((f"/admin/users/{nina}/screens", {"mode": "auto"}), ("/admin/segments/healthcare/screens", {}),
                       ("/admin/ai/bulk", {"grant": "1"})):
        assert w.client.post(path, data={"csrf": t2, **data}).status_code == 404
