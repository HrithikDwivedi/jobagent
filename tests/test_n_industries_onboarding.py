"""N. Every-industry job search: taxonomy + classification, onboarding questions (resume, industries, roles,
experience, countries), discovery scope and new sources (Jobicy, Adzuna), career-aware scoring, the personal
"For you" feed, the administrator's audience segments, and removal of closed jobs. All data is SYNTHETIC."""
from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest

from conftest import TEST_PASSWORD, extract_csrf, web_factory
from factories import ADMIN, make_user, pdf_bytes
from jobagent import agent, onboarding, taxonomy
from jobagent import settings as settings_mod
from jobagent.db import iso, utcnow
from jobagent.http import HttpClient
from jobagent.ingest import (close_job, expire_unseen, posting_closed, purge_closed, role_in_scope, upsert_raw)
from jobagent.matching import candidate_profile, score_job
from jobagent.sources import AdzunaSource, JobicySource, RawJob, build_sources
from webdata import insert_job


# =============================================================================== taxonomy

@pytest.mark.parametrize("title,expected", [
    ("Senior Backend Engineer (Python)", ("technology", "software_engineer")),
    ("Registered Nurse - ICU", ("healthcare", "nurse")),
    ("Accounts Payable Assistant", ("finance", "accountant")),
    ("Account Executive, EMEA", ("sales", "sales_executive")),
    ("Sales Engineer", ("sales", "presales")),
    ("Primary School Teacher", ("education", "teacher")),
    ("Talent Acquisition Partner", ("hr", "recruiter")),
    ("Warehouse Operative", ("operations", "logistics")),
    ("Customer Service Advisor", ("customer_service", "customer_support")),
    ("Graphic Designer", ("design", "graphic_designer")),
    ("Civil Engineer", ("engineering", "civil")),
    ("Paralegal", ("legal", "paralegal")),
    ("Executive Assistant to the CEO", ("admin", "executive_assistant")),
    ("Store Manager", ("hospitality", "store_manager")),
    ("Business Analyst", ("business", "business_analyst")),
    ("Software Developer (m/w/d)", ("technology", "software_engineer")),
])
def test_classify_titles_across_industries(title, expected):
    assert taxonomy.classify(title) == expected


def test_classify_fallbacks_and_unknowns():
    assert taxonomy.classify("Werkstudent Marketing") == ("marketing", None)          # industry word in title
    assert taxonomy.classify("Associate", ["Finance & Accounting"]) == ("finance", None)   # source category
    assert taxonomy.classify("Operative") == (None, None)
    assert taxonomy.classify("") == (None, None) and taxonomy.classify(None) == (None, None)


def test_every_role_is_reachable_and_ids_are_unique():
    ids = [r.id for i in taxonomy.INDUSTRIES for r in i.roles]
    assert len(ids) == len(set(ids))
    for ind in taxonomy.INDUSTRIES:
        for r in ind.roles:
            got = taxonomy.classify(r.query)
            assert got[0] is not None, (r.id, r.query)


def test_persona_labels():
    nurse = {"industries": ["healthcare"], "roles": ["nurse"], "experience": "entry"}
    p = taxonomy.persona(nurse, resume_industry="technology", needs_sponsorship=True)
    assert p["label"] == "Early-career · Healthcare & Life Sciences · Nurse"
    assert p["tags"] == ["Career switcher", "Needs visa sponsorship"]
    p = taxonomy.persona({}, inferred_from=["backend engineer", ".net developer"])
    assert p["industry"] == "technology" and "Inferred" in p["tags"]
    assert taxonomy.persona({})["label"] == "Not set up yet"


def test_level_excludes_never_block_chosen_roles():
    c = {"roles": ["store_manager"], "custom_roles": []}
    assert "manager" not in taxonomy.excluded_terms_for("student", c)
    assert "senior" in taxonomy.excluded_terms_for("entry", c)
    assert "intern" in taxonomy.excluded_terms_for("senior", {"roles": []})


# =============================================================================== career setting

@pytest.mark.parametrize("value,msg", [
    ({"industries": ["healthcare", "finance", "sales", "hr"]}, "at most 3 industries"),
    ({"industries": ["space-piracy"]}, "Unknown industry"),
    ({"industries": ["healthcare"], "roles": ["accountant"]}, "not in the industries you chose"),
    ({"industries": ["healthcare"], "roles": ["nurse", "doctor", "pharmacist"], "custom_roles": ["Ward Sister"]},
     "at most 3 roles"),
    ({"industries": ["healthcare"], "custom_roles": ["<script>"]}, "letters, digits"),
    ({"industries": ["healthcare"], "experience": "wizard"}, "experience level"),
    ({"industries": ["healthcare"], "employment_types": ["slavery"]}, "employment type"),
])
def test_career_validation(value, msg):
    with pytest.raises(settings_mod.SettingsError, match=msg):
        settings_mod.validate("career", value)


def test_career_validation_normalises():
    v = settings_mod.validate("career", {"industries": ["healthcare", "healthcare"], "roles": ["nurse"],
                                         "custom_roles": ["  Ward   Sister ", "ward sister", ""]})
    assert v["industries"] == ["healthcare"] and v["custom_roles"] == ["Ward Sister"]
    assert v["employment_types"] == ["full_time"]


# =============================================================================== discovery scope

def _raw(title, **kw):
    return RawJob(source=kw.pop("source", "jobicy"), source_job_id=kw.pop("sid", title), title=title,
                  company=kw.pop("company", "Acme"), location_raw=kw.pop("loc", "London, United Kingdom"),
                  url=kw.pop("url", f"https://jobs.example/{abs(hash(title))}"), apply_url=None,
                  description_text=kw.pop("desc", "A great job."), **kw)


def test_scope_legacy_users_keep_software_only():
    assert role_in_scope(_raw("Backend Engineer"), None)[0]
    assert not role_in_scope(_raw("Registered Nurse"), None)[0]
    assert not role_in_scope(_raw("Registered Nurse"), {"legacy": True, "industries": [], "roles": [],
                                                        "custom": []})[0]


def test_scope_follows_what_users_chose():
    scope = {"legacy": True, "industries": ["healthcare"], "roles": ["nurse"], "custom": ["Ward Sister"]}
    assert role_in_scope(_raw("Staff Nurse"), scope)[0]
    assert role_in_scope(_raw("Ward Sister - Cardiology"), scope)[0]                  # typed role
    assert role_in_scope(_raw("Backend Engineer"), scope)[0]                          # a legacy user's software
    assert not role_in_scope(_raw("Pharmacist"), scope)[0]                            # same industry, not wanted
    assert not role_in_scope(_raw("Accountant"), scope)[0]
    hinted = _raw("Specialist", extra={"industries": ["Healthcare & Medical"]})
    assert role_in_scope(hinted, scope)[0]                                            # industry from the source


def test_discovery_settings_union(anydb):
    db = anydb
    bea, cara = make_user(db, "bea"), make_user(db, "cara")
    settings_mod.set_value(db, "career", {"industries": ["healthcare"], "roles": ["nurse"]}, "x", user_id=bea)
    settings_mod.set_value(db, "career", {"industries": ["finance", "healthcare"], "roles": ["accountant"],
                                          "custom_roles": ["Tax Associate"]}, "x", user_id=cara)
    sc = settings_mod.discovery_settings(db)["career_scope"]
    assert sc["legacy"] is True                                       # the admin has no career profile
    assert sc["industries"] == ["healthcare", "finance"] and sc["roles"] == ["nurse", "accountant"]
    assert sc["custom"] == ["Tax Associate"]
    assert ("healthcare", "nurse") in sc["queries"] and ("finance", "Tax Associate") in sc["queries"]
    p = settings_mod.personal_discovery_settings(db, bea)["career_scope"]
    assert p["roles"] == ["nurse"] and p["legacy"] is False


def test_upsert_stores_classification(db):
    s = settings_mod.discovery_settings(db)
    s["career_scope"] = {"legacy": False, "industries": ["healthcare"], "roles": ["nurse"], "custom": []}
    decision, jid, _ = upsert_raw(db, _raw("Registered Nurse (Band 5)"), s)
    assert decision == "new"
    assert db.one("SELECT industry, role_key FROM jobs WHERE id=?", (jid,)) == {"industry": "healthcare",
                                                                                "role_key": "nurse"}
    assert upsert_raw(db, _raw("Chef de Partie"), s)[0] == "out_of_scope"


# =============================================================================== sources

JOBICY = {"jobs": [
    {"id": 101, "url": "https://jobicy.com/jobs/101-registered-nurse", "jobTitle": "Registered Nurse &amp; Educator",
     "companyName": "CareCo", "jobIndustry": ["Healthcare & Medical"], "jobType": ["Full-Time"], "jobGeo": "UK",
     "jobLevel": "Midweight", "jobDescription": "<p>Patient care. Visa sponsorship available.</p>",
     "pubDate": "2026-11-18 09:00:00", "annualSalaryMin": 30000, "annualSalaryMax": 38000, "salaryCurrency": "GBP"},
    {"id": 102, "url": "https://jobicy.com/jobs/102", "jobTitle": "Clinical Data Manager", "companyName": "Pharm",
     "jobIndustry": ["Healthcare & Medical"], "jobGeo": "Anywhere", "jobDescription": "x", "pubDate": "bad"},
    {"id": None, "jobTitle": "no id"}, "junk"]}


def test_jobicy_parsing():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, json=JOBICY)
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    res = JobicySource(http).fetch("healthcare")
    assert res.error is None and res.complete is False and len(res.jobs) == 2
    j = res.jobs[0]
    assert j.title == "Registered Nurse & Educator" and j.location_raw == "Remote - UK"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (30000.0, 38000.0, "GBP")
    assert j.extra["industries"] == ["Healthcare & Medical"] and res.jobs[1].location_raw == "Remote"
    assert seen[0].params["industry"] == "healthcare" and seen[0].params["count"] == "50"


def test_jobicy_errors():
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"oops": 1})),
                      sleep=lambda s: None)
    assert JobicySource(http).fetch("x").error == "Malformed Jobicy response"
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=lambda s: None)
    assert "404" in JobicySource(http).fetch("x").error


def test_adzuna_parsing_and_key_redaction():
    page = {"results": [{"id": "77", "title": "<strong>Accountant</strong>", "company": {"display_name": "Ledger Ltd"},
                         "location": {"display_name": "Leeds", "area": ["UK", "West Yorkshire", "Leeds"]},
                         "description": "Month end close.", "redirect_url": "https://adzuna.example/77",
                         "created": "2026-11-17T10:00:00Z", "salary_min": 32000, "salary_max": 36000,
                         "contract_time": "full_time", "category": {"label": "Accounting & Finance Jobs"}}]}
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page)), sleep=lambda s: None)
    res = AdzunaSource(http, "ID123", "KEY456").fetch("gb|accountant")
    j = res.jobs[0]
    assert (j.title, j.company, j.location_raw, j.country_hint) == ("Accountant", "Ledger Ltd", "Leeds, West Yorkshire",
                                                                     "GB")
    assert j.employment_type == "full_time" and j.extra["category"] == "Accounting & Finance Jobs"
    assert AdzunaSource(http, "a", "b").fetch("xx|nurse").error == "invalid Adzuna query"

    def bad(req):
        return httpx.Response(401, text="nope")
    res = AdzunaSource(HttpClient(transport=httpx.MockTransport(bad), sleep=lambda s: None), "ID123", "KEY456")\
        .fetch("gb|nurse")
    assert res.error and "KEY456" not in res.error and "ID123" not in res.error


def test_build_sources_targets_chosen_industries_and_countries():
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), sleep=lambda s: None)
    conf = settings_mod.DEFAULT_SETTINGS["sources"]
    scope = {"industries": ["healthcare", "finance"], "roles": ["nurse"], "custom": [],
             "queries": [("healthcare", "nurse"), ("finance", "accountant")]}
    items = build_sources(http, conf, scope, ["GB", "IE", "IN"])
    jobicy = [b for s, b in items if s.name == "jobicy"]
    assert jobicy == ["healthcare", "accounting-finance"]
    assert not [1 for s, _ in items if s.name == "adzuna"]                       # no keys = no Adzuna
    items = build_sources(http, conf, scope, ["GB", "IE", "IN"], adzuna_keys=("i", "k"))
    az = [b for s, b in items if s.name == "adzuna"]
    assert az == ["gb|nurse", "in|nurse", "gb|accountant", "in|accountant"]      # IE is not an Adzuna country
    assert len([b for s, b in build_sources(http, conf, scope, ["GB", "IN"], ("i", "k"), max_queries=1)
                if s.name == "adzuna"]) == 1
    assert [s.name for s, _ in build_sources(http, conf, None)] == [s.name for s, _ in build_sources(http, conf)]


# =============================================================================== closed jobs

@pytest.mark.parametrize("text,closed", [
    ("This job is no longer accepting applications.", True),
    ("Unfortunately this vacancy has been filled.", True),
    ("Applications are now closed.", True),
    ("Closing date: 1 November 2026.", True),
    ("Application deadline: November 30th, 2026", False),     # today in the test is 2026-11-19
    ("We accept applications until the position has been filled.", False),
    ("Open until the role is filled.", False),
    ("A great role. Apply now!", False),
])
def test_posting_closed_detection(text, closed):
    from datetime import date
    assert bool(posting_closed("Nurse", text, today=date(2026, 11, 19))) is closed


def test_closed_postings_are_not_stored_and_close_known_jobs(db):
    s = settings_mod.discovery_settings(db)
    raw = _raw("Backend Engineer", sid="gh-1", desc="Python services.")
    _, jid, _ = upsert_raw(db, raw, s)
    db.run("INSERT INTO applications(job_id, user_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
           (jid, ADMIN, "ready_to_apply", iso(), iso()))
    raw.description_text = "Python services. This position is no longer accepting applications."
    decision, jid2, why = upsert_raw(db, raw, s)
    assert jid2 == jid and "no longer accepting" in why
    assert db.scalar("SELECT status FROM jobs WHERE id=?", (jid,)) == "closed"
    assert db.scalar("SELECT status FROM applications WHERE job_id=?", (jid,)) == "closed"
    assert upsert_raw(db, _raw("Frontend Engineer", sid="gh-2", desc="Applications are closed."), s)[0] == \
        "out_of_scope"


def test_expire_and_purge_lifecycle(db):
    old = iso(utcnow() - timedelta(days=40))
    stale = insert_job(db, title="Old Engineer")
    fresh = insert_job(db, title="Fresh Engineer")
    kept = insert_job(db, title="Applied Engineer", app_status="submitted")
    marked = insert_job(db, title="Bookmarked Engineer", bookmarked=1)
    for jid in (stale, kept, marked):
        db.run("UPDATE jobs SET last_seen_at=? WHERE id=?", (old, jid))
    with db.transaction():
        db.run("INSERT INTO job_scores(job_id, user_id, score, recommendation, breakdown_json, details_json, "
               "weights_json, created_at) VALUES (?,?,50,'review','{}','{}','{}',?)", (stale, ADMIN, iso()))
    expired = expire_unseen(db)
    assert set(expired) == {stale, kept, marked}
    assert db.scalar("SELECT status FROM jobs WHERE id=?", (fresh,)) == "open"
    assert db.scalar("SELECT status FROM applications WHERE job_id=?", (kept,)) == "submitted"   # history kept
    assert purge_closed(db) == 0                                         # closed only just now
    db.run("UPDATE jobs SET last_verified_at=? WHERE id IN (?,?,?)", (old, stale, kept, marked))
    assert purge_closed(db) == 1
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE id=?", (stale,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM job_scores WHERE job_id=?", (stale,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE id IN (?,?)", (kept, marked)) == 2


def test_verify_job_page_check_for_feed_sources(db):
    jid = insert_job(db, title="Nurse", apply_url="https://jobicy.com/jobs/9")
    with db.transaction():
        db.run("INSERT INTO job_listings(job_id, source, source_job_id, url, canonical_url, first_seen_at, "
               "last_seen_at) VALUES (?,?,?,?,?,?,?)", (jid, "jobicy", "9", "https://jobicy.com/jobs/9",
                                                        "https://jobicy.com/jobs/9", iso(), iso()))
    pages = {"text": "<h1>Nurse</h1><p>Apply now</p>", "status": 200}

    def handler(req):
        return httpx.Response(pages["status"], text=pages["text"], headers={"content-type": "text/html"})
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert agent.verify_job(db, jid, http) == "open"
    pages["text"] = "<h1>Nurse</h1><p>Sorry, this job is no longer accepting applications.</p>"
    assert agent.verify_job(db, jid, http) == "closed"
    jid2 = insert_job(db, title="Nurse 2", apply_url="https://jobicy.com/jobs/10")
    with db.transaction():
        db.run("INSERT INTO job_listings(job_id, source, source_job_id, url, canonical_url, first_seen_at, "
               "last_seen_at) VALUES (?,?,?,?,?,?,?)", (jid2, "jobicy", "10", "https://jobicy.com/jobs/10",
                                                        "https://jobicy.com/jobs/10", iso(), iso()))
    pages.update(status=404, text="gone")
    assert agent.verify_job(db, jid2, http) == "closed"
    reasons = [r["detail_json"] for r in db.all("SELECT detail_json FROM audit_events WHERE action='job_closed'")]
    assert any("404" in r for r in reasons) and any("no longer accepting" in r for r in reasons)


# =============================================================================== scoring

def _settings(**career):
    s = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
    s["career"] = settings_mod.validate("career", career)
    s["excluded_title_terms"] = taxonomy.excluded_terms_for(s["career"]["experience"], s["career"])
    s["target_countries"] = {"tier1": ["GB"], "tier2": [], "tier3": []}
    return s


def _score(job, s, resume=None):
    from jobagent import jd
    prof = candidate_profile(resume or {}, {}, s["candidate"])
    sp = {"classification": "unknown", "needs_sponsorship": False}
    return score_job(job, jd.analyze(job["title"], job.get("description_text", "")), sp, prof, s)


def test_nurse_feed_scores_nursing_jobs_not_software():
    s = _settings(industries=["healthcare"], roles=["nurse"], experience="entry")
    nurse = {"title": "Registered Nurse", "industry": "healthcare", "role_key": "nurse", "country": "GB",
             "description_text": "Patient care, wards, medication rounds and care plans."}
    dev = {"title": "Software Engineer", "industry": "technology", "role_key": "software_engineer", "country": "GB",
           "description_text": "Python."}
    resume = {"experience": [{"title": "Healthcare Assistant", "bullets": ["Patient care on busy wards",
                                                                          "Medication rounds and care plans"]}]}
    a, b = _score(nurse, s, resume), _score(dev, s, resume)
    assert a["components"]["role"] == 1.0 and b["components"]["role"] == 0.1
    assert a["score"] > b["score"] + 25
    assert a["components"]["technical"] > 0.6 and any("key words" in r for r in a["reasons"])
    assert a["recommendation"] in ("apply", "review") and not a["disqualifiers"]
    assert not any("software" in d.lower() for d in a["disqualifiers"] + a["gaps"])


def test_level_rules_for_every_industry():
    entry = _settings(industries=["hospitality"], roles=["retail"], experience="entry")
    senior_job = {"title": "Senior Retail Manager", "industry": "hospitality", "country": "GB", "description_text": ""}
    assert _score(senior_job, entry)["disqualifiers"]
    mgr = _settings(industries=["hospitality"], roles=["store_manager"], experience="manager")
    store = {"title": "Store Manager", "industry": "hospitality", "role_key": "store_manager", "country": "GB",
             "description_text": "Lead the team."}
    r = _score(store, mgr)
    assert not r["disqualifiers"] and r["components"]["experience"] >= 0.95
    student = _settings(industries=["marketing"], roles=["digital_marketing"], experience="student",
                        employment_types=["internship", "part_time"])
    intern = {"title": "Marketing Intern", "industry": "marketing", "role_key": "digital_marketing", "country": "GB",
              "employment_type": "internship", "description_text": ""}
    assert not any("Internship" in d for d in _score(intern, student)["disqualifiers"])
    no_resume = _score(store, mgr)
    assert no_resume["components"]["technical"] == 0.5 and any("Upload your resume" in n for n in no_resume["notes"])


def test_legacy_profile_scoring_unchanged():
    s = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
    r = _score({"title": "Registered Nurse", "country": "GB", "description_text": ""}, s)
    assert any("Not a software engineering role" in d for d in r["disqualifiers"])


# =============================================================================== onboarding (web)

def _jobicy_transport(jobs_by_industry):
    def handler(req):
        if "jobicy.com" in req.url.host:
            return httpx.Response(200, json={"jobs": jobs_by_industry.get(req.url.params.get("industry"), [])})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


NURSE_JOBS = [
    {"id": 501, "url": "https://jobicy.com/jobs/501", "jobTitle": "Registered Nurse", "companyName": "NHS Partner",
     "jobIndustry": ["Healthcare & Medical"], "jobType": ["Full-Time"], "jobGeo": "UK",
     "jobDescription": "Patient care on wards. Medication rounds.", "pubDate": "2026-11-18 09:00:00"},
    {"id": 502, "url": "https://jobicy.com/jobs/502", "jobTitle": "Pharmacist", "companyName": "Chem",
     "jobIndustry": ["Healthcare & Medical"], "jobGeo": "UK", "jobDescription": "Dispensing.",
     "pubDate": "2026-11-18 09:00:00"},
    {"id": 503, "url": "https://jobicy.com/jobs/503", "jobTitle": "Staff Nurse (closed)", "companyName": "Ward Co",
     "jobIndustry": ["Healthcare & Medical"], "jobGeo": "UK",
     "jobDescription": "This job is no longer accepting applications.", "pubDate": "2026-11-18 09:00:00"},
]


@pytest.fixture
def newbie(tmp_path):
    """A web app with an admin and a freshly signed-up, approved user 'nina' who has not onboarded."""
    w = web_factory(tmp_path)
    w.setup_user()
    r = w.signup("nina", full_name="Nina Nurse", email="nina@example.com")
    assert r.status_code in (200, 303)
    d = w.db()
    from jobagent import users as users_mod
    nina = d.scalar("SELECT id FROM users WHERE username='nina'")
    users_mod.change_status(d, nina, "approve", "test")
    insert_job(d, title="Backend Engineer", company="CodeCo", country="GB")
    d.close()
    w.client.app.state.http_factory = lambda: HttpClient(transport=_jobicy_transport({"healthcare": NURSE_JOBS}),
                                                         sleep=lambda s: None)
    assert w.login("nina", TEST_PASSWORD).status_code == 303
    w.nina = nina
    return w


def test_new_user_is_sent_to_onboarding_first(newbie):
    c = newbie.client
    for path in ("/", "/jobs", "/settings", "/applications/1", "/admin/users"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/welcome", path
    assert c.get("/support").status_code == 200                      # help stays reachable
    r = c.get("/welcome", follow_redirects=False)
    assert r.headers["location"] == "/welcome/1"
    page = c.get("/welcome/1").text
    assert "Welcome, Nina" in page and "Skip for now" in page and 'class="sidebar"' not in page
    # a POST elsewhere is redirected too (nothing changes)
    tok = extract_csrf(page)
    r = c.post("/settings", data={"csrf": tok, "section": "candidate"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    # roles cannot be chosen before industries
    assert c.get("/welcome/3", follow_redirects=False).headers["location"] == "/welcome/2"
    assert c.get("/welcome/9").status_code == 404


def test_onboarding_validation(newbie):
    c = newbie.client
    tok = extract_csrf(c.get("/welcome/2").text)
    r = c.post("/welcome/2", data={"csrf": tok})
    assert r.status_code == 400 and "at least one industry" in r.text
    r = c.post("/welcome/2", data={"csrf": tok, "industry": ["healthcare", "finance", "sales", "hr"]})
    assert r.status_code == 400 and "at most 3" in r.text
    r = c.post("/welcome/2", data={"csrf": tok, "industry": ["nonsense"]})
    assert r.status_code == 400 and "Unknown industry" in r.text
    assert c.post("/welcome/2", data={"industry": ["healthcare"]}).status_code == 403      # CSRF
    assert c.post("/welcome/2", data={"csrf": tok, "industry": ["healthcare", "finance"]},
                  follow_redirects=False).headers["location"] == "/welcome/3"
    page = c.get("/welcome/3").text
    assert "Registered" not in page and "Nurse" in page and "Accountant" in page and "Software Engineer" not in page
    r = c.post("/welcome/3", data={"csrf": tok})
    assert r.status_code == 400 and "at least one role" in r.text
    r = c.post("/welcome/3", data={"csrf": tok, "role": ["teacher"]})
    assert r.status_code == 400 and "not in the industries" in r.text
    r = c.post("/welcome/3", data={"csrf": tok, "role": ["nurse", "doctor", "accountant"], "custom_role": "Ward Sister"})
    assert r.status_code == 400 and "at most 3 roles" in r.text
    assert c.post("/welcome/3", data={"csrf": tok, "role": ["nurse"], "custom_role": "Ward Sister"},
                  follow_redirects=False).headers["location"] == "/welcome/4"
    r = c.post("/welcome/4", data={"csrf": tok, "employment_type": ["full_time"]})
    assert r.status_code == 400 and "experience level" in r.text
    r = c.post("/welcome/4", data={"csrf": tok, "experience": "entry"})
    assert r.status_code == 400 and "type of job" in r.text
    assert c.post("/welcome/4", data={"csrf": tok, "experience": "entry", "employment_type": ["full_time"]},
                  follow_redirects=False).headers["location"] == "/welcome/5"
    base = {"csrf": tok, "country": ["GB"], "arrangement": ["onsite"], "sponsorship": "no"}
    for bad, msg in ((dict(base, country=[]), "at least one country"),
                     (dict(base, country=["GB", "IE", "DE", "FR", "NL", "ES", "IT", "PL", "SE"]), "at most 8"),
                     (dict(base, country=["ZZ"]), "Unknown country"),
                     (dict(base, arrangement=[]), "way of working"),
                     (dict(base, sponsorship=""), "visa sponsorship"),
                     (dict(base, sponsorship="some"), "already work"),
                     (dict(base, min_salary="lots"), "must be a number")):
        r = c.post("/welcome/5", data=bad)
        assert r.status_code == 400 and msg in r.text, msg
    # still not onboarded after failed attempts
    assert c.get("/jobs", follow_redirects=False).headers["location"] == "/welcome"


def test_onboarding_end_to_end_builds_a_personal_feed(newbie):
    c, d = newbie.client, newbie.db()
    tok = extract_csrf(c.get("/welcome/1").text)
    c.post("/welcome/2", data={"csrf": tok, "industry": ["healthcare"]})
    c.post("/welcome/3", data={"csrf": tok, "role": ["nurse"]})
    c.post("/welcome/4", data={"csrf": tok, "experience": "entry", "employment_type": ["full_time", "part_time"]})
    r = c.post("/welcome/5", data={"csrf": tok, "country": ["GB", "IE"], "arrangement": ["onsite", "hybrid", "remote"],
                                   "sponsorship": "some", "authorised": ["GB"], "relocate": "1",
                                   "current_location": "Leeds, United Kingdom", "min_salary": "28,000",
                                   "currency": "GBP"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    s = settings_mod.get_all(d, newbie.nina)
    assert s["career"]["onboarded_at"] and s["career"]["roles"] == ["nurse"]
    assert s["target_countries"]["tier1"] == ["GB", "IE"] and s["include_india_remote"] is False
    assert s["candidate"]["work_authorization"] == {"GB": "work_permit"}
    assert s["candidate"]["requires_sponsorship_outside"] is True and s["candidate"]["full_name"] == "Nina Nurse"
    assert s["min_salary"] == 28000 and s["currency"] == "GBP" and "nurse" in s["preferred_roles"]
    assert "senior" in s["excluded_title_terms"]
    assert d.scalar("SELECT onboarded_at FROM users WHERE id=?", (newbie.nina,)) is not None
    # the personal search fetched healthcare jobs; the closed posting was never stored
    titles = {r["title"] for r in d.all("SELECT title FROM jobs")}
    assert "Registered Nurse" in titles and "Staff Nurse (closed)" not in titles
    assert "Pharmacist" not in titles                                  # not a role anyone chose
    run = d.one("SELECT trigger FROM runs ORDER BY started_at DESC LIMIT 1")
    assert run["trigger"] == "onboarding"
    d.close()
    page = c.get("/jobs").text
    assert "Registered Nurse" in page and "Backend Engineer" not in page and "Your feed:" in page
    assert "Backend Engineer" in c.get("/jobs", params={"view": "all"}).text
    assert "Registered Nurse" in c.get("/jobs", params={"view": "all", "industry": "healthcare"}).text
    assert "Backend Engineer" not in c.get("/jobs", params={"view": "all", "industry": "healthcare"}).text
    home = c.get("/").text
    assert "Searching for <strong>Nurse</strong>" in home
    assert 'href="/mnc"' not in home                                  # Top MNCs is not for a nurse
    assert 'href="/sponsorship"' in home                              # needs sponsorship outside GB
    # editing later never locks the user out
    assert c.get("/welcome", follow_redirects=False).headers["location"] == "/welcome/2"
    assert "Back to dashboard" in c.get("/welcome/2").text


def test_resume_step_suggests_industries(newbie):
    c = newbie.client
    tok = extract_csrf(c.get("/welcome/1").text)
    r = c.post("/welcome/1", data={"csrf": tok}, files={"resume": ("cv.pdf", pdf_bytes(), "application/pdf")},
               follow_redirects=False)
    assert r.headers["location"] == "/welcome/2"
    page = c.get("/welcome/2").text
    assert "From your resume" in page
    assert re.search(r'value="technology" checked', page)                 # the synthetic resume is a developer's
    r = c.post("/welcome/1", data={"csrf": tok}, files={"resume": ("cv.png", b"\x89PNG....", "image/png")})
    assert r.status_code == 400
    # skipping the upload is fine
    assert c.post("/welcome/1", data={"csrf": tok}, follow_redirects=False).headers["location"] == "/welcome/2"


def test_no_sponsorship_hides_sponsorship_pages_and_existing_accounts_are_not_forced(newbie):
    c = newbie.client
    tok = extract_csrf(c.get("/welcome/1").text)
    c.post("/welcome/2", data={"csrf": tok, "industry": ["finance"]})
    c.post("/welcome/3", data={"csrf": tok, "role": ["accountant"]})
    c.post("/welcome/4", data={"csrf": tok, "experience": "mid", "employment_type": ["full_time"]})
    c.post("/welcome/5", data={"csrf": tok, "country": ["GB"], "arrangement": ["hybrid"], "sponsorship": "no"})
    home = c.get("/").text
    assert 'href="/sponsorship"' not in home and 'href="/mnc"' in home        # banks are in Top MNCs
    d = newbie.db()
    s = settings_mod.get_all(d, newbie.nina)
    assert s["sponsorship_policy"] == "any" and s["candidate"]["requires_sponsorship_outside"] is False
    d.close()
    # the admin (created by setup) was never forced through onboarding but is invited to it
    newbie.client.cookies.clear()
    assert newbie.login().status_code == 303
    home = newbie.client.get("/").text
    assert "Set your job preferences" in home


def test_personal_search_survives_source_failures(newbie):
    newbie.client.app.state.http_factory = lambda: HttpClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)), sleep=lambda s: None, max_retries=0)
    c = newbie.client
    tok = extract_csrf(c.get("/welcome/1").text)
    c.post("/welcome/2", data={"csrf": tok, "industry": ["healthcare"]})
    c.post("/welcome/3", data={"csrf": tok, "role": ["nurse"]})
    c.post("/welcome/4", data={"csrf": tok, "experience": "entry", "employment_type": ["full_time"]})
    r = c.post("/welcome/5", data={"csrf": tok, "country": ["GB"], "arrangement": ["remote"], "sponsorship": "no"},
               follow_redirects=True)
    assert r.status_code == 200 and "next agent run" in r.text                # preferences saved anyway
    assert c.get("/", follow_redirects=False).status_code == 200


def test_user_without_resume_still_gets_ranked_jobs(anydb):
    db = anydb
    uid = make_user(db, "nina")
    settings_mod.set_value(db, "career", {"industries": ["healthcare"], "roles": ["nurse"], "experience": "entry"},
                           "x", user_id=uid)
    settings_mod.set_value(db, "target_countries", {"tier1": ["GB"], "tier2": [], "tier3": []}, "x", user_id=uid)
    jid = insert_job(db, title="Registered Nurse", country="GB", description="Patient care")
    other = insert_job(db, title="Backend Engineer", country="GB")
    db.run("UPDATE jobs SET industry='healthcare', role_key='nurse' WHERE id=?", (jid,))
    db.run("UPDATE jobs SET industry='technology', role_key='software_engineer' WHERE id=?", (other,))
    from jobagent.config import load_config
    cfg = load_config()
    cfg.database_url, cfg.secret_key, cfg.resume_path = "sqlite:///unused", "k" * 40, None
    out = agent.run_agent(db, cfg, discover=False, user_ids=[uid], prepare=True)
    assert out["status"] in ("succeeded", "partial")
    scored = {r["job_id"] for r in db.all("SELECT job_id FROM job_scores WHERE user_id=?", (uid,))}
    assert jid in scored and other not in scored                       # only the user's own industries
    assert db.scalar("SELECT COUNT(*) FROM applications WHERE user_id=?", (uid,)) == 0   # nothing prepared


# =============================================================================== admin: audience

def test_admin_audience_segments_and_personas(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    assert w.login().status_code == 303
    d = w.db()
    for name, career, need in (("nina", {"industries": ["healthcare"], "roles": ["nurse"], "experience": "entry"}, True),
                               ("noah", {"industries": ["healthcare"], "roles": ["pharmacist"], "experience": "senior"},
                                False),
                               ("fin", {"industries": ["finance"], "roles": ["accountant"], "experience": "mid"}, False)):
        uid = make_user(d, name)
        settings_mod.set_value(d, "career", career, "x", user_id=uid)
        cand = settings_mod.get(d, "candidate", uid)
        settings_mod.set_value(d, "candidate", {**cand, "requires_sponsorship_outside": need}, "x", user_id=uid)
        settings_mod.set_value(d, "target_countries", {"tier1": ["GB"], "tier2": [], "tier3": []}, "x", user_id=uid)
    legacy = make_user(d, "leo")
    settings_mod.set_value(d, "preferred_roles", ["backend engineer"], "x", user_id=legacy)
    for i in range(40):
        jid = insert_job(d, title=f"Accountant {i}", country="GB")
        d.run("UPDATE jobs SET industry='finance', role_key='accountant' WHERE id=?", (jid,))
    d.close()
    page = w.client.get("/admin/users").text
    assert "Users &amp; audience" in page
    assert "Healthcare &amp; Life Sciences" in page and "Finance, Banking &amp; Accounting" in page
    assert "Low supply" in page and "ADZUNA_APP_ID" in page                      # healthcare has no jobs
    assert "Good supply" in page or "Some supply" in page                       # finance: 40 jobs / 1 user
    assert "Early-career · Healthcare &amp; Life Sciences · Nurse" in page
    assert "Needs visa sponsorship" in page and "Inferred" in page
    seg = w.client.get("/admin/users", params={"segment": "finance"}).text
    table = seg.split('id="all-users"', 1)[1]
    assert "fin@example.com" in table and "nina@example.com" not in table
    unset = w.client.get("/admin/users", params={"segment": "unset"}).text.split('id="all-users"', 1)[1]
    assert "leo@example.com" in unset and "fin@example.com" not in unset
    assert w.client.get("/admin/users", params={"segment": "<x>"}).status_code == 200
    # non-admins never see it
    w.client.cookies.clear()
    assert w.login("nina", TEST_PASSWORD).status_code == 303
    assert w.client.get("/admin/users").status_code == 404
