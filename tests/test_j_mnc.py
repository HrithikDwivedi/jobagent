"""J. Top-MNC feeds, employer tagging and the /mnc page. All HTTP is mocked (SYNTHETIC payloads)."""
from __future__ import annotations

import httpx
import pytest

from factories import ADMIN

from jobagent import mnc
from jobagent import settings as settings_mod
from jobagent.http import HttpClient
from jobagent.ingest import upsert_raw
from jobagent.web import queries
from tests.factories import make_raw


def client_for(handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, min_interval=0)


def robots_ok(request):
    return httpx.Response(200, text="User-agent: *\nAllow: /\n")


def company(key):
    c = mnc.by_key(key)
    assert c, key
    return c


# ---------------------------------------------------------------- catalogue & matching

def test_catalog_loads_and_keys_unique():
    """Catalogue loads; keys are unique; every entry has a careers URL."""
    keys = [c["key"] for c in mnc.catalog()]
    assert len(keys) == len(set(keys)) and len(keys) >= 30
    assert all(c["careers_url"].startswith("https://") for c in mnc.catalog())


@pytest.mark.parametrize("name,key", [("Amazon.com Services LLC", "amazon"), ("Barclays Bank PLC", "barclays"),
                                      ("JPMorgan Chase & Co.", "jpmorgan"), ("Deloitte Touche Tohmatsu", "deloitte"),
                                      ("Stripe", "stripe"), ("Capgemini Technology Services India", "capgemini")])
def test_match_employer_aliases(name, key):
    """Legal-entity names resolve to the catalogue company."""
    assert (mnc.match_employer(name) or {}).get("key") == key


def test_match_employer_no_false_positive():
    """Unrelated employers are not tagged as MNCs."""
    assert mnc.match_employer("Acme Widgets Ltd") is None
    assert mnc.match_employer("Amazonas Software GmbH") is None


@pytest.mark.parametrize("title,ok", [("Software Engineer II", True), ("Software Development Engineer", True),
                                      ("Management Consultant - Software & Platforms", False),
                                      ("Software Architect", False), ("Patent Engineer", False),
                                      ("Java Developer", True)])
def test_is_software_title(title, ok):
    """MNC title filter keeps individual-contributor software roles only."""
    assert mnc.is_software_title(title) is ok


def test_google_has_no_feed_because_robots_disallow():
    """Google is link-only (its robots.txt disallows automated job search)."""
    g = company("google")
    assert g["feed"] is None and "robots" in g["why_no_feed"]


# ---------------------------------------------------------------- adapters

def test_workday_adapter_list_and_detail():
    """Workday: lists postings, fetches details, keeps software titles, prefixes ids with the company key."""
    def handler(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return robots_ok(req)
        if req.method == "POST" and req.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"total": 2, "jobPostings": [
                {"title": "Software Engineer", "externalPath": "/job/Pune/Software-Engineer_JR-1", "locationsText": "Pune",
                 "bulletFields": ["JR-0001"]},
                {"title": "Relationship Manager", "externalPath": "/job/x/RM_JR-2", "locationsText": "London"}]})
        if req.url.path.endswith("/job/Pune/Software-Engineer_JR-1"):
            return httpx.Response(200, json={"jobPostingInfo": {
                "title": "Software Engineer", "jobDescription": "<p>Build C# services</p>", "location": "Pune",
                "country": {"descriptor": "India"}, "jobReqId": "JR-0001", "startDate": "2026-11-01",
                "timeType": "Full time", "externalUrl": "https://barclays.wd3.myworkdayjobs.com/x/job/1"}})
        return httpx.Response(404)
    http = client_for(handler)
    src = mnc.WorkdaySource(http, company("barclays"), mnc.RobotsCache(http), max_pages=1)
    res = src.fetch()
    assert res.complete and not res.error and len(res.jobs) == 1
    j = res.jobs[0]
    assert j.source_job_id == "barclays:JR-0001" and j.company == "Barclays" and j.requisition_id == "JR-0001"
    assert "C# services" in j.description_text and j.country_hint == "India"
    assert j.extra["mnc_key"] == "barclays"


def test_robots_disallow_blocks_feed():
    """If robots.txt disallows the endpoint, nothing is fetched and the source reports why."""
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, json={"jobs": []})
    http = client_for(handler)
    res = mnc.AmazonSource(http, company("amazon"), mnc.RobotsCache(http)).fetch()
    assert not res.jobs and "robots.txt disallows" in (res.error or "")
    assert calls == ["/robots.txt"]


def test_robots_404_means_allowed():
    """RFC 9309: a missing robots.txt (4xx) means no restrictions."""
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json={"count": 1, "data": [{"id": "1-en", "title": "Software Engineer",
                                                                "country_code": "en-in", "location": "Pune",
                                                                "description_stripped": "Java and SQL",
                                                                "apply_job_url": "https://careers.capgemini.com/job/1"}]})
    http = client_for(handler)
    res = mnc.CapgeminiSource(http, company("capgemini"), mnc.RobotsCache(http), max_pages=1).fetch()
    assert len(res.jobs) == 1 and res.jobs[0].country_hint == "IN"


def test_amazon_adapter_maps_fields():
    """Amazon: ISO3 country -> ISO2, job page used as apply URL (same host as the listing)."""
    def handler(req):
        if req.url.path == "/robots.txt":
            return robots_ok(req)
        return httpx.Response(200, json={"jobs": [{"id_icims": "123", "title": "Software Development Engineer",
                                                   "job_path": "/en/jobs/123/sde", "normalized_location": "Dublin, IRL",
                                                   "country_code": "IRL", "description": "Java", "posted_date": "November  3, 2026"}]})
    http = client_for(handler)
    res = mnc.AmazonSource(http, company("amazon"), mnc.RobotsCache(http), max_pages=1).fetch(countries=["IE"])
    j = res.jobs[0]
    assert j.country_hint == "IE" and j.apply_url == "https://www.amazon.jobs/en/jobs/123/sde"
    assert j.published_at.startswith("2026-11-03")


def test_malformed_feed_is_an_error_not_exception():
    """Malformed JSON gives a SourceResult error, never a crash."""
    def handler(req):
        if req.url.path == "/robots.txt":
            return robots_ok(req)
        return httpx.Response(200, text="<html>oops</html>")
    http = client_for(handler)
    res = mnc.AtlassianSource(http, company("atlassian"), mnc.RobotsCache(http)).fetch()
    assert not res.jobs and res.error


# ---------------------------------------------------------------- ingestion, filters, settings, page

def test_ingest_tags_mnc_employer_from_feed_and_alias(db):
    """Feed jobs carry mnc_key; existing-board jobs (e.g. Stripe on Greenhouse) are tagged by alias."""
    s = settings_mod.get_all(db, ADMIN)
    d1, j1, _ = upsert_raw(db, make_raw(n=1, company="Barclays", source="workday", source_job_id="barclays:1",
                                        title="Software Engineer", location_raw="Pune, India",
                                        url="https://barclays.wd3.myworkdayjobs.com/x/1",
                                        extra={"mnc_key": "barclays"}), s)
    d2, j2, _ = upsert_raw(db, make_raw(n=2, company="Stripe", source_job_id="stripe:2"), s)
    d3, j3, _ = upsert_raw(db, make_raw(n=3, company="Acme", source_job_id="acme:3", title="Backend Engineer"), s)
    tags = {r["name"]: r["mnc_key"] for r in db.all("SELECT name, mnc_key FROM employers")}
    assert tags == {"Barclays": "barclays", "Stripe": "stripe", "Acme": None}


def test_mnc_filter_in_search(db):
    """mnc=all returns only MNC jobs; mnc=<key> narrows to that company."""
    s = settings_mod.get_all(db, ADMIN)
    upsert_raw(db, make_raw(n=1, company="Barclays", source_job_id="b:1", extra={"mnc_key": "barclays"}), s)
    upsert_raw(db, make_raw(n=2, company="Stripe", source_job_id="s:2", title="Backend Engineer"), s)
    upsert_raw(db, make_raw(n=3, company="Acme", source_job_id="a:3", title="Platform Engineer"), s)
    f = queries.JobFilters(mnc=["all"])
    rows, total = queries.search_jobs(db, f, user_id=ADMIN)
    assert total == 2 and {r["employer"] for r in rows} == {"Barclays", "Stripe"}
    rows, total = queries.search_jobs(db, queries.JobFilters(mnc=["barclays"]), user_id=ADMIN)
    assert total == 1 and rows[0]["mnc_key"] == "barclays"


def test_mnc_overview_counts(db):
    """Overview lists every catalogue company with open-job counts, sorted by count."""
    s = settings_mod.get_all(db, ADMIN)
    upsert_raw(db, make_raw(n=1, company="Barclays", source_job_id="b:1", extra={"mnc_key": "barclays"}), s)
    ov = queries.mnc_overview(db, user_id=ADMIN)
    assert ov[0]["key"] == "barclays" and ov[0]["open_jobs"] == 1
    assert {c["key"] for c in ov} == {c["key"] for c in mnc.catalog()}


def test_mnc_settings_validation():
    """Unknown companies are rejected; defaults are valid."""
    assert settings_mod.validate("mnc", settings_mod.DEFAULT_SETTINGS["mnc"])["enabled"] is True
    with pytest.raises(settings_mod.SettingsError):
        settings_mod.validate("mnc", {"companies": ["not-a-company"]})


def test_build_mnc_sources_respects_selection():
    """Only selected companies with a permitted feed become sources; link-only companies are skipped."""
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=lambda s: None)
    items = mnc.build_mnc_sources(http, {"enabled": True, "companies": ["amazon", "google", "barclays"]}, ["IE"])
    assert sorted(k for _, k in items) == ["amazon", "barclays"]
    assert mnc.build_mnc_sources(http, {"enabled": False}, ["IE"]) == []


def test_mnc_page_requires_login(web):
    """The Top MNCs page is protected like every other page."""
    r = web.client.get("/mnc", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_mnc_page_renders_companies_and_jobs(client):
    """Logged in: company cards, careers links (https, noopener) and the MNC job table render."""
    db = client.db()
    s = settings_mod.get_all(db, ADMIN)
    upsert_raw(db, make_raw(n=1, company="Barclays", source_job_id="b:1", extra={"mnc_key": "barclays"}), s)
    r = client.client.get("/mnc")
    assert r.status_code == 200
    assert "Barclays" in r.text and "Google" in r.text and "Morgan Stanley" in r.text
    assert 'rel="noopener noreferrer"' in r.text and "/jobs?mnc=barclays" in r.text
    r2 = client.client.get("/jobs?mnc=all")
    assert r2.status_code == 200 and "Top MNCs only" in r2.text
