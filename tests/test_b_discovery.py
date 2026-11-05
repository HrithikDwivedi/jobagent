"""B. Discovery & normalisation (mock HTTP only, SYNTHETIC listings)."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from factories import ADMIN, FakeSource, make_raw, no_network_transport, agent_config
from jobagent import agent
from jobagent import settings as settings_mod
from jobagent.db import iso, utcnow
from jobagent.http import FetchError, HttpClient, RateLimited
from jobagent.ingest import mark_closures, upsert_raw
from jobagent.jd import analyze
from jobagent.matching import score_job
from jobagent.normalize import canonical_url
from jobagent.sources import ArbeitnowSource, GreenhouseSource
from jobagent.sponsorship import classify


def S(db):
    return settings_mod.get_all(db, ADMIN)


def http(handler, **kw):
    sleeps = []
    c = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: sleeps.append(s), **kw)
    c.sleeps = sleeps
    return c


def count(db, table):
    return db.scalar(f"SELECT COUNT(*) FROM {table}")


# ----------------------------------------------------------------------------- URLs

def test_canonical_url_strips_tracking_keeps_identity():
    """Expected: utm_*, gh_src, fragments, www and trailing slash are removed; gh_jid is kept."""
    u = "HTTPS://www.Example.com/careers/job/?utm_source=x&gh_src=abc&gh_jid=123&utm_medium=y#apply"
    assert canonical_url(u) == "https://example.com/careers/job?gh_jid=123"
    assert canonical_url("https://example.com/a/") == canonical_url("https://example.com/a")
    assert canonical_url("javascript:alert(1)") is None


def test_same_requisition_different_urls_one_job(db):
    """Expected: two listings with different URLs but the same employer requisition ID map to ONE job."""
    a = make_raw(n=1, requisition_id="REQ-100", url="https://boards.greenhouse.io/acme/jobs/1?gh_src=li")
    b = make_raw(source="lever", source_job_id="acme:zz", requisition_id="REQ-100",
                 title="Software Engineer (m/f/d)", url="https://jobs.lever.co/acme/zz")
    d1, j1, _ = upsert_raw(db, a, S(db))
    d2, j2, _ = upsert_raw(db, b, S(db))
    assert (d1, j1) == ("new", j1) and j1 == j2 and d2 in ("duplicate", "updated")
    assert count(db, "jobs") == 1 and count(db, "job_listings") == 2


def test_same_canonical_url_with_tracking_is_same_listing(db):
    """Expected: the same URL with extra tracking parameters is recognised as the same listing."""
    upsert_raw(db, make_raw(n=1), S(db))
    d, _, _ = upsert_raw(db, make_raw(source="arbeitnow", source_job_id="slug-1",
                                      url="https://boards.greenhouse.io/acme/jobs/1/?utm_source=feed#x"), S(db))
    assert d in ("duplicate", "updated") and count(db, "jobs") == 1


def test_redirects_followed():
    """Expected: HttpClient follows a 301 redirect and returns the final 200 JSON."""
    def h(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://api.example.com/new"})
        return httpx.Response(200, json={"ok": True})
    c = http(h)
    assert c.get_json("https://api.example.com/old") == {"ok": True}


def test_duplicate_listings_across_boards(db):
    """Expected: same employer+title+country on two boards -> one job with two listings."""
    upsert_raw(db, make_raw(n=1, company="Acme Ltd"), S(db))
    d, _, _ = upsert_raw(db, make_raw(source="arbeitnow", source_job_id="acme-se-dublin", company="ACME Inc.",
                                      url="https://www.arbeitnow.com/jobs/acme-se-dublin"), S(db))
    assert d in ("duplicate", "updated")
    assert count(db, "jobs") == 1 and count(db, "job_listings") == 2


def test_different_roles_same_employer_distinct(db):
    """Expected: different roles at one employer remain separate jobs."""
    upsert_raw(db, make_raw(n=1, title="Software Engineer"), S(db))
    upsert_raw(db, make_raw(n=2, title="Python Developer"), S(db))
    assert count(db, "jobs") == 2


def test_similar_titles_distinct(db):
    """Expected: 'Backend Engineer' and 'Frontend Engineer' are different jobs."""
    upsert_raw(db, make_raw(n=1, title="Backend Engineer"), S(db))
    d, _, _ = upsert_raw(db, make_raw(n=2, title="Frontend Engineer"), S(db))
    assert d == "new" and count(db, "jobs") == 2


def test_different_requisitions_same_title_two_jobs(db):
    """Expected: same title with different requisition IDs -> two jobs."""
    upsert_raw(db, make_raw(n=1, requisition_id="R-1"), S(db))
    d, _, _ = upsert_raw(db, make_raw(n=2, requisition_id="R-2"), S(db))
    assert d == "new" and count(db, "jobs") == 2


def test_repost_after_closure_reopened(db):
    """Expected: a repost of a closed vacancy with the same requisition reopens the SAME job (not new)."""
    _, jid, _ = upsert_raw(db, make_raw(n=1, requisition_id="R-9"), S(db))
    db.run("UPDATE jobs SET status='closed', last_seen_at=? WHERE id=?", (iso(utcnow() - timedelta(days=90)), jid))
    d, jid2, _ = upsert_raw(db, make_raw(n=77, requisition_id="R-9", description_text="Reposted text C# .NET"), S(db))
    assert d == "reopened" and jid2 == jid
    assert db.scalar("SELECT status FROM jobs WHERE id=?", (jid,)) == "open"


def test_mark_closures_complete_fetch(db):
    """Expected: after a COMPLETE board fetch, a job missing from the board is closed; seen jobs stay open."""
    _, j1, _ = upsert_raw(db, make_raw(n=1, title="Software Engineer"), S(db))
    _, j2, _ = upsert_raw(db, make_raw(n=2, title="Python Developer"), S(db))
    closed = mark_closures(db, "greenhouse", "acme", {"acme:1"}, iso())
    assert closed == [j2]
    assert db.scalar("SELECT status FROM jobs WHERE id=?", (j1,)) == "open"


def _agent_settings(db):
    settings_mod.set_value(db, "sources", {"uk_sponsor_register": {"enabled": False}}, user_id=ADMIN)


@pytest.mark.parametrize("complete", [True, False])
def test_closures_only_after_complete_fetch(db, monkeypatch, complete):
    """Expected: a missing job is closed by an agent run only if the source fetch was complete."""
    _agent_settings(db)
    _, j2, _ = upsert_raw(db, make_raw(n=2, title="Python Developer"), S(db))
    src = FakeSource(jobs=[make_raw(n=1)], complete=complete)
    monkeypatch.setattr(agent, "build_sources", lambda h, c: [(src, "acme")])
    agent.run_agent(db, agent_config("x"), http=HttpClient(transport=no_network_transport(), sleep=lambda s: None))
    assert db.scalar("SELECT status FROM jobs WHERE id=?", (j2,)) == ("closed" if complete else "open")


def test_missing_salary_ok(db):
    """Expected: a listing without salary is stored with NULL salary fields."""
    d, jid, _ = upsert_raw(db, make_raw(), S(db))
    row = db.one("SELECT salary_min, salary_max, salary_source FROM jobs WHERE id=?", (jid,))
    assert d == "new" and row == {"salary_min": None, "salary_max": None, "salary_source": None}


def test_missing_publication_date_ok(db):
    """Expected: published_at=None is accepted (first_discovered_at is still set)."""
    d, jid, _ = upsert_raw(db, make_raw(published_at=None), S(db))
    row = db.one("SELECT published_at, first_discovered_at FROM jobs WHERE id=?", (jid,))
    assert d == "new" and row["published_at"] is None and row["first_discovered_at"]


def test_conflicting_location_reported(db):
    """Expected: structured country hint wins over conflicting text and the conflict is reported."""
    d, jid, reason = upsert_raw(db, make_raw(location_raw="Berlin, Germany", country_hint="IE"), S(db))
    assert d == "new" and "conflicts" in reason
    assert db.scalar("SELECT country FROM jobs WHERE id=?", (jid,)) == "IE"


def test_unknown_sponsorship():
    """Expected: a posting with no sponsorship statement is classified 'unknown'."""
    r = classify("We build APIs in C#.", country="IE", candidate={"work_authorization": {"IN": "citizen"}})
    assert r["classification"] == "unknown" and r["needs_sponsorship"]


def test_unsupported_local_language_disqualifies():
    """Expected: a required German (C1) posting produces a language disqualifier and recommendation 'skip'."""
    text = "Requirements\n• C# and .NET\n• Fluent German (C1) is required for this role.\n"
    an = analyze("Software Engineer", text)
    assert any(l["language"] == "German" and l["required"] for l in an["languages"])
    profile = {"skills": {"C#": "demonstrated", ".NET": "demonstrated"}, "experience_years": 2.0,
               "languages": {"english": "C1"}, "work_authorization": {"IN": "citizen"}}
    sp = {"classification": "confirmed", "needs_sponsorship": True}
    res = score_job({"title": "Software Engineer", "country": "DE", "remote_type": "onsite"}, an, sp, profile,
                    settings_mod.DEFAULT_SETTINGS)
    assert any("German" in d for d in res["disqualifiers"]) and res["recommendation"] == "skip"


@pytest.mark.parametrize("body,expect", [
    ("{not json", "Malformed JSON"),
    ({"unexpected": True}, "Malformed Greenhouse response"),
])
def test_malformed_api_json(body, expect):
    """Expected: malformed provider JSON yields a SourceResult with an error and complete=False (no exception)."""
    def h(req):
        return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(200, text=body)
    res = GreenhouseSource(http(h)).fetch("acme")
    assert res.jobs == [] and res.complete is False and expect in res.error


def test_provider_timeout_bounded_retries():
    """Expected: repeated timeouts raise a retryable FetchError after max_retries+1 attempts."""
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)
    c = http(h, max_retries=2)
    with pytest.raises(FetchError) as ei:
        c.get("https://api.example.com/x")
    assert ei.value.retryable and c.stats["requests"] == 3 and c.stats["retries"] == 2


def test_429_retry_after_honoured():
    """Expected: 429 with Retry-After: 2 sleeps 2s then succeeds."""
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": 1})
    c = http(h)
    assert c.get_json("https://api.example.com/x") == {"ok": 1}
    assert 2.0 in c.sleeps and c.stats["rate_limited"] == 1


def test_429_huge_retry_after_gives_up():
    """Expected: a Retry-After longer than max_delay makes the client give up immediately with RateLimited."""
    def h(req):
        return httpx.Response(429, headers={"Retry-After": "3600"})
    c = http(h)
    with pytest.raises(RateLimited):
        c.get("https://api.example.com/x")
    assert c.stats["requests"] == 1 and 3600 not in c.sleeps


def test_empty_results():
    """Expected: an empty job board returns zero jobs and complete=True."""
    def h(req):
        return httpx.Response(200, json={"name": "Acme", "jobs": []} if req.url.path.endswith("/jobs")
                              else {"name": "Acme"})
    res = GreenhouseSource(http(h)).fetch("acme")
    assert res.jobs == [] and res.complete and res.error is None


def _arbeit_page(i, nxt):
    return {"data": [{"slug": f"job-{i}", "title": "Software Engineer", "company_name": "Acme",
                      "location": "Berlin", "url": f"https://www.arbeitnow.com/jobs/job-{i}",
                      "description": "<p>C#</p>", "created_at": 1700000000}],
            "links": {"next": nxt}}


def test_arbeitnow_pagination_max_pages_incomplete():
    """Expected: hitting max_pages while more pages exist returns the jobs fetched and complete=False."""
    def h(req):
        page = int(req.url.params.get("page", "1"))
        return httpx.Response(200, json=_arbeit_page(page, f"https://www.arbeitnow.com/api/job-board-api?page={page + 1}"))
    res = ArbeitnowSource(http(h), max_pages=2).fetch()
    assert [j.source_job_id for j in res.jobs] == ["job-1", "job-2"] and res.complete is False


def test_arbeitnow_partial_failure():
    """Expected: a failing second page keeps page-1 jobs, records the error and marks complete=False."""
    def h(req):
        if req.url.params.get("page") == "2":
            return httpx.Response(404)
        return httpx.Response(200, json=_arbeit_page(1, "https://www.arbeitnow.com/api/job-board-api?page=2"))
    res = ArbeitnowSource(http(h, max_retries=0), max_pages=5).fetch()
    assert len(res.jobs) == 1 and res.complete is False and "404" in res.error


def test_upsert_idempotent(db):
    """Expected: upserting the same raw listing twice returns 'duplicate' and counts stay at 1."""
    raw = make_raw()
    assert upsert_raw(db, raw, S(db))[0] == "new"
    assert upsert_raw(db, raw, S(db))[0] == "duplicate"
    assert count(db, "jobs") == 1 and count(db, "job_listings") == 1 and count(db, "employers") == 1


@pytest.mark.parametrize("title", ["Patent Engineer", "Mechanical Engineer", "Sales Manager",
                                   "Senior Hardware Engineer", "Firmware Engineer (Embedded)"])
def test_out_of_scope_titles_filtered(db, title):
    """Expected: non-software / non-engineering titles are returned as out_of_scope and not stored."""
    d, jid, _ = upsert_raw(db, make_raw(title=title), S(db))
    assert d == "out_of_scope" and jid is None and count(db, "jobs") == 0


def test_outside_target_country_filtered(db):
    """Expected: a software role located only outside target countries (e.g. Brazil) is out_of_scope."""
    d, _, why = upsert_raw(db, make_raw(location_raw="São Paulo, Brazil"), S(db))
    assert d == "out_of_scope" and "outside" in why
