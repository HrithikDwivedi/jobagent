"""F. Dashboard & usability (TestClient, SYNTHETIC data seeded via SQL)."""
from __future__ import annotations

import time
import uuid
from urllib.parse import quote

import pytest

from conftest import TEST_PASSWORD, TEST_USER, web_factory
from jobagent.db import iso
from jobagent.web import charts
from webdata import LabelChecker, insert_job

EMPTY_PAGES = ["/", "/jobs", "/applications", "/studio", "/runs", "/prep", "/sponsorship", "/audit", "/settings"]


@pytest.mark.parametrize("path", EMPTY_PAGES)
def test_empty_database_pages_render(client, path):
    """Expected: every main page renders 200 on an empty database."""
    r = client.client.get(path)
    assert r.status_code == 200 and "Something went wrong" not in r.text


def test_large_dataset_paginates_fast(client):
    """Expected: with 1500 jobs, /jobs renders page 1 in <3s, totals are correct and pages slice correctly."""
    d = client.db()
    emp = d.insert("INSERT INTO employers(name, normalized_name, created_at) VALUES ('BigCo','bigco',?)", (iso(),))
    with d.transaction():
        for i in range(1500):
            d.run("INSERT INTO jobs(job_key, employer_id, title, normalized_title, country, seniority, remote_type, "
                  "first_discovered_at, last_seen_at, status) VALUES (?,?,?,?, 'IE', 'junior', 'onsite', ?, ?, 'open')",
                  (uuid.uuid4().hex, emp, f"Software Engineer {i}", f"software engineer {i}", iso(), iso()))
    d.close()
    t = time.perf_counter()
    r = client.client.get("/jobs", params={"per_page": 25})
    assert r.status_code == 200 and time.perf_counter() - t < 3.0
    assert "1500" in r.text.replace(",", "")
    api = client.client.get("/api/jobs", params={"per_page": 25, "page": 60}).json()
    assert api["total"] == 1500 and len(api["jobs"]) == 25
    assert client.client.get("/api/jobs", params={"per_page": 25, "page": 61}).json()["jobs"] == []


def test_long_strings_render(client):
    """Expected: very long employer and title strings render without error."""
    d = client.db()
    jid = insert_job(d, title="Software Engineer " + "X" * 280, company="Company" + "Y" * 190)
    d.close()
    for p in ("/jobs", f"/jobs/{jid}"):
        r = client.client.get(p)
        assert r.status_code == 200 and "YYYYYYYYYY" in r.text


def test_missing_data_renders_dash(client):
    """Expected: a job with no salary/city/country/date renders '—' placeholders without errors."""
    d = client.db()
    jid = insert_job(d, country=None, city=None, published_hours_ago=None, employment_type=None)
    d.close()
    r = client.client.get(f"/jobs/{jid}")
    assert r.status_code == 200 and "—" in r.text


def test_international_characters_render(client):
    """Expected: München, São Paulo, 日本語 and emoji render intact."""
    d = client.db()
    jid = insert_job(d, title="Software Engineer München 日本語 🚀", company="São Paulo Tech", city="München")
    d.close()
    for p in ("/jobs", f"/jobs/{jid}"):
        r = client.client.get(p)
        assert r.status_code == 200
        for s in ("München", "日本語", "🚀", "São Paulo Tech"):
            assert s in r.text


def seed_filters(d):
    j1 = insert_job(d, "Alpha Backend Engineer", "Zeta Corp", country="IE", remote_type="onsite",
                    employment_type="full_time", seniority="junior", salary_min=50000, salary_max=60000,
                    salary_currency="EUR", published_hours_ago=1, score=90, rec="apply", sponsorship="confirmed",
                    bookmarked=1)
    j2 = insert_job(d, "Beta Python Developer", "Acme", country="DE", city="Berlin", remote_type="remote",
                    employment_type="contract", seniority="mid", published_hours_ago=24 * 10, score=50, rec="review",
                    sponsorship="not_supported", app_status="submitted")
    j3 = insert_job(d, "Gamma .NET Developer", "Midway", country="GB", city="London", remote_type="hybrid",
                    employment_type="internship", seniority="intern", salary_max=30000, salary_currency="GBP",
                    published_hours_ago=24 * 40, score=70, rec="apply", sponsorship="likely", status="closed")
    return j1, j2, j3


A, B, G = "Alpha Backend Engineer", "Beta Python Developer", "Gamma .NET Developer"


@pytest.mark.parametrize("params,expected", [
    ({"date_posted": "24h"}, {A}), ({"date_posted": "month"}, {A, B}), ({"experience": "entry"}, {A}),
    ({"experience": "mid"}, {B}), ({"job_type": "contract"}, {B}), ({"remote": "remote"}, {B}),
    ({"country": "DE"}, {B}), ({"sponsorship": "confirmed"}, {A}), ({"rec": "review"}, {B}),
    ({"status": "discovered"}, {A}), ({"status": "submitted"}, {B}), ({"company": "zeta"}, {A}),
    ({"salary_min": "55000"}, {A}), ({"has_salary": "1"}, {A}), ({"min_score": "60"}, {A}),
    ({"bookmarked": "1"}, {A}), ({"include_closed": "1"}, {A, B, G}), ({"visa_friendly": "1"}, {A}),
    ({"visa_friendly": "1", "include_closed": "1"}, {A, G}), ({"q": "python"}, {B}), ({"location": "berlin"}, {B}),
    ({"experience": "internship", "include_closed": "1"}, {G}),
])
def test_filters(client, params, expected):
    """Expected: each filter param returns 200 on /jobs and exactly the matching jobs via the same query."""
    d = client.db()
    seed_filters(d)
    d.close()
    assert client.client.get("/jobs", params=params).status_code == 200
    got = {j["title"] for j in client.client.get("/api/jobs", params=params).json()["jobs"]}
    assert got == expected


@pytest.mark.parametrize("sort,order", [("recent", [A, B]), ("company", [B, A]), ("relevant", [A, B]),
                                        ("salary", [A, B])])
def test_sorting(client, sort, order):
    """Expected: every sort option returns 200 with the expected order."""
    d = client.db()
    seed_filters(d)
    d.close()
    assert client.client.get("/jobs", params={"sort": sort}).status_code == 200
    assert [j["title"] for j in client.client.get("/api/jobs", params={"sort": sort}).json()["jobs"]] == order


def test_pagination_links(client):
    """Expected: with 25 jobs and per_page=10 the page shows a link to page 2 and page 3 lists 5 jobs."""
    d = client.db()
    for i in range(25):
        insert_job(d, f"Software Engineer {i}", "Acme")
    d.close()
    r = client.client.get("/jobs", params={"per_page": 10})
    assert r.status_code == 200 and "page=2" in r.text
    assert len(client.client.get("/api/jobs", params={"per_page": 10, "page": 3}).json()["jobs"]) == 5


def test_csv_export_formula_injection(client):
    """Expected: CSV export is text/csv with a header and neutralises cells starting with '=' (apostrophe prefix)."""
    d = client.db()
    insert_job(d, "=HYPERLINK(\"http://evil\") Software Engineer", "Acme")
    insert_job(d, "Normal Engineer", "+SUM(1)")
    d.close()
    r = client.client.get("/jobs/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0].startswith("id,employer,title")
    assert "'=HYPERLINK" in r.text and "'+SUM(1)" in r.text
    assert ",=HYPERLINK" not in r.text and ",\"=HYPERLINK" not in r.text


def test_charts_empty_datasets():
    """Expected: charts with empty/zero data return the accessible 'No data yet' element."""
    for html in (charts.hbar([], "X"), charts.hbar([("a", 0)], "X"), charts.columns([("w1", 0), ("w2", 0)], "Y")):
        assert "No data yet" in html and 'role="img"' in html


def test_deep_link_after_expiry_returns_to_page(web):
    """Expected: an anonymous deep link redirects to /login?next=<path>, and logging in returns to that path."""
    web.setup_user()
    r = web.client.get("/jobs?country=IE", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=" + quote("/jobs?country=IE")
    r = web.login(next_url="/jobs?country=IE")
    assert r.status_code == 303 and r.headers["location"] == "/jobs?country=IE"


def test_open_redirect_blocked(web):
    """Expected: a next= pointing off-site is replaced by '/'."""
    web.setup_user()
    r = web.login(next_url="//evil.example.com/x")
    assert r.status_code == 303 and r.headers["location"] == "/"


@pytest.mark.parametrize("path", ["/settings", "/jobs", "/login"])
def test_accessibility_basics(client, path):
    """Expected: every visible form control has a label/aria-label; pages declare <html lang> (and a skip link when signed in)."""
    if path == "/login":
        client.client.post("/logout", data={"csrf": client.csrf("/")})
    r = client.client.get(path)
    assert r.status_code == 200
    p = LabelChecker()
    p.feed(r.text)
    assert p.html_lang, "missing <html lang>"
    assert p.unlabeled() == []
    if path != "/login":
        assert p.skip_link


def test_db_unavailable_503(tmp_path):
    """Expected: an unreachable database yields a 503 plain-text message, not a stack trace."""
    blocker = tmp_path / "f.txt"
    blocker.write_text("x")
    w = web_factory(tmp_path, database_url="sqlite:///" + str(blocker / "sub" / "x.db"))
    with w.client:
        r = w.client.get("/login")
    assert r.status_code == 503 and "Database unavailable" in r.text and "Traceback" not in r.text


@pytest.mark.parametrize("path", ["/jobs/99999", "/applications/99999", "/runs/nope", "/letters/99999"])
def test_unknown_entity_404(client, path):
    """Expected: unknown jobs/applications/runs/letters render a 404 page."""
    r = client.client.get(path)
    assert r.status_code == 404 and "not found" in r.text.lower()
