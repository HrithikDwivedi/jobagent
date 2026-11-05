"""Render-smoke test: renders every dashboard page on a temporary SQLite DB with an authenticated session and
fails on template errors, missing pages, or CSP violations (inline styles/scripts/event handlers).

    .venv\\Scripts\\python.exe scripts\\render_smoke.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from jobagent import agent  # noqa: E402
from jobagent import applications as apps  # noqa: E402
from jobagent import settings as settings_mod  # noqa: E402
from jobagent import users as users_mod  # noqa: E402
from jobagent.config import load_config  # noqa: E402
from jobagent.db import Database, iso  # noqa: E402
from jobagent.ingest import upsert_raw  # noqa: E402
from jobagent.matching import candidate_profile  # noqa: E402
from jobagent.resume import store as resume_store  # noqa: E402
from jobagent.sources import RawJob  # noqa: E402
from jobagent.web.app import create_app  # noqa: E402

RESUME = r"C:\Users\tledt\OneDrive\Desktop\hrithik\Hrithik_Dwivedi_Resume_India.pdf"
USER, PASSWORD = "smoketest", "Sm0ke-Test-Passw0rd!"
USER2 = "smokeuser"
ADMIN_ID = 1
FAILURES: list[str] = []

CSP_PATTERNS = [
    (re.compile(r"\sstyle\s*=", re.I), "inline style attribute"),
    (re.compile(r"<style[\s>]", re.I), "<style> block"),
    (re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I), "inline <script>"),
    (re.compile(r"<[a-z][^>]*\s on[a-z]+\s*=", re.I), "inline event handler"),
    (re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.I), "inline event handler"),
    (re.compile(r"javascript:", re.I), "javascript: URL"),
]


def check_html(name: str, html: str) -> None:
    for marker in ("jinja2", "Traceback", "UndefinedError", "Something went wrong"):
        if marker in html:
            FAILURES.append(f"{name}: contains '{marker}'")
    # strip server-generated SVG charts (they have no styles/scripts, but keep the check focused on templates)
    for rx, what in CSP_PATTERNS:
        m = rx.search(html)
        if m:
            FAILURES.append(f"{name}: {what}: ...{html[max(0, m.start() - 60):m.end() + 60]!r}...")
    for m in re.finditer(r'<a [^>]*target="_blank"[^>]*>', html):
        if 'rel="noopener noreferrer"' not in m.group(0):
            FAILURES.append(f"{name}: target=_blank link without rel=noopener noreferrer: {m.group(0)}")
    for m in re.finditer(r'<form[^>]*method="post"[^>]*>(.*?)</form>', html, re.S | re.I):
        body = m.group(1)
        if 'name="csrf"' not in body and 'name="pre_csrf"' not in body:
            FAILURES.append(f"{name}: POST form without csrf: {m.group(0)[:160]}")


def get(client: TestClient, url: str, name: str | None = None, expect: int = 200) -> str:
    name = name or url
    r = client.get(url, follow_redirects=False)
    if r.status_code != expect:
        FAILURES.append(f"{name}: HTTP {r.status_code} (expected {expect}): {r.text[:300]}")
        print(f"FAIL {name} ({r.status_code})")
        return r.text
    check_html(name, r.text)
    print(f"OK   {name}")
    return r.text


def pre_csrf(html: str) -> str:
    m = re.search(r'name="pre_csrf" value="([^"]+)"', html)
    assert m, "pre_csrf not found"
    return m.group(1)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="jobagent-smoke-")
    cfg = load_config()
    cfg.database_url = f"sqlite:///{Path(tmp, 't.db').as_posix()}"
    cfg.secret_key = "x" * 40
    # never send real email from the smoke test (console provider only records to the outbox)
    cfg.resend_api_key, cfg.smtp_host = None, None
    cfg.admin_email, cfg.public_url = "smoke-admin@example.com", "http://localhost:8765"
    app = create_app(cfg)

    with TestClient(app) as client:
        # ---------------- first-run setup + login pages
        html = get(client, "/setup", "setup page")
        r = client.post("/setup", data={"pre_csrf": pre_csrf(html), "username": USER, "password": PASSWORD,
                                        "password2": PASSWORD}, follow_redirects=False)
        assert r.status_code == 303, f"setup failed: {r.status_code} {r.text[:300]}"
        html = get(client, "/login", "login page")
        r = client.post("/login", data={"pre_csrf": pre_csrf(html), "username": USER, "password": PASSWORD,
                                        "next": "/"}, follow_redirects=False)
        assert r.status_code == 303, f"login failed: {r.status_code} {r.text[:300]}"

        # ---------------- public sign-up flow (second user stays pending until approved below)
        with TestClient(app) as anon:
            html = get(anon, "/signup", "signup page")
            r = anon.post("/signup", data={"pre_csrf": pre_csrf(html), "full_name": "Smoke User",
                                           "email": "smoke.user@example.com", "username": USER2,
                                           "password": PASSWORD, "password2": PASSWORD}, follow_redirects=False)
            if r.status_code != 200 or "awaiting approval" not in r.text.lower():
                FAILURES.append(f"signup submit: HTTP {r.status_code}")
            else:
                check_html("signup done page", r.text)
                print("OK   signup done page")
            html = get(anon, "/login", "login page (anonymous)")
            r = anon.post("/login", data={"pre_csrf": pre_csrf(html), "username": USER2, "password": PASSWORD,
                                          "next": "/"}, follow_redirects=False)
            if r.status_code != 403 or "awaiting approval" not in r.text.lower():
                FAILURES.append(f"pending login: HTTP {r.status_code}")
            else:
                check_html("login page (pending notice)", r.text)
                print("OK   login page (pending notice)")
            sdb = Database(cfg.database_url)
            try:
                user2_id = sdb.scalar("SELECT id FROM users WHERE username=?", (USER2,))
                tokens = users_mod.create_review_tokens(sdb, cfg.secret_key, user2_id)
            finally:
                sdb.close()
            get(anon, f"/signup/review/{tokens['approve']}", "signup review (approve)")
            get(anon, f"/signup/review/{tokens['reject']}", "signup review (reject)")
            get(anon, "/signup/review/not-a-token", "signup review (invalid)", expect=400)

        # ---------------- empty database pages
        get(client, "/", "overview (empty db)")
        get(client, "/admin/users", "admin users (pending)")
        get(client, "/jobs", "jobs (empty db)")
        get(client, "/applications", "applications (empty db)")
        get(client, "/studio", "studio (empty db)")
        get(client, "/prep", "prep (empty db)")
        get(client, "/sponsorship", "sponsorship (empty db)")
        get(client, "/jobs/compare", "compare (empty)")
        get(client, "/jobs/999999", "job not found", expect=404)

        # ---------------- seed data
        db = Database(cfg.database_url)
        try:
            resume = resume_store.import_original(db, RESUME, user_id=ADMIN_ID)
            s = settings_mod.get_all(db, ADMIN_ID)
            now = iso()
            raws = [
                RawJob(source="greenhouse", source_job_id="smoke-1", title=".NET Software Engineer",
                       company="Acme Payments Ltd", location_raw="Dublin, Ireland",
                       url="https://boards.greenhouse.io/acme/jobs/1", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                       description_text=("We build payment APIs in C#, .NET, ASP.NET Core and SQL on AWS.\n"
                                         "Requirements: 2+ years of experience with C# and REST APIs.\n"
                                         "Visa sponsorship is available for this role. Relocation package offered.\n"
                                         "Please include a cover letter. Nice to have: Docker, Kubernetes."),
                       published_at=now, employment_type="full_time", salary_min=55000, salary_max=70000,
                       salary_currency="EUR", salary_period="year"),
                RawJob(source="greenhouse", source_job_id="smoke-2", title="Senior Backend Engineer (Go) — Zahlungsverkehr",
                       company="Berliner Bank Tech GmbH", location_raw="Berlin, Germany",
                       url="https://boards.greenhouse.io/bbt/jobs/2", apply_url="https://boards.greenhouse.io/bbt/jobs/2",
                       description_text=("Backend in Go and Kafka. 6+ years of experience required.\n"
                                         "Fluent German (C1) is required. We are unable to sponsor visas.\n"
                                         "Ünïcödé test: Straße, 東京, <script>alert(1)</script>"),
                       published_at=now, employment_type="full_time"),
                RawJob(source="lever", source_job_id="smoke-3", title="Python Software Engineer",
                       company="Thames Data Ltd", location_raw="London, United Kingdom",
                       url="https://jobs.lever.co/thames/3", apply_url="https://jobs.lever.co/thames/3/apply",
                       description_text=("Python, FastAPI, SQL, Docker. 1+ years of experience.\n"
                                         "We can sponsor Skilled Worker visas."),
                       published_at=now, employment_type="full_time"),
            ]
            ids = []
            for raw in raws:
                decision, job_id, reason = upsert_raw(db, raw, s)
                assert job_id, f"upsert_raw rejected {raw.title}: {decision} {reason}"
                ids.append(job_id)
            with db.transaction():
                db.run("UPDATE jobs SET is_sample=0, last_verified_at=?", (iso(),))
            profile = candidate_profile(resume["structured"], resume["analysis"], s["candidate"])
            pv = agent.profile_version(resume, s)
            for jid in ids:
                agent.score_and_store(db, jid, profile, s, pv, user_id=ADMIN_ID)
            app1 = agent.prepare_application(db, ids[0], f"user:{USER}", profile, s, None, user_id=ADMIN_ID)
            # Drive a second application through approval -> start -> submitted so post-submit views render.
            app3 = agent.prepare_application(db, ids[2], f"user:{USER}", profile, s, None, user_id=ADMIN_ID)
            a3 = db.one("SELECT * FROM applications WHERE id=?", (app3["id"],))
            gate = apps.approval_gate_checks(db, a3, s)
            submitted_id = None
            if not gate:
                fp = apps.fingerprint(apps.snapshot(db, a3))
                apps.approve(db, a3["id"], f"user:{USER}", fp, user_id=ADMIN_ID)
                apps.start_application(db, a3["id"], f"user:{USER}", user_id=ADMIN_ID)
                apps.record_outcome(db, a3["id"], "submitted", f"user:{USER}", user_id=ADMIN_ID, reference_number="REF-12345",
                                    confirmation_text="Thank you for applying!")
                apps.update_progress(db, a3["id"], f"user:{USER}", "interview", user_id=ADMIN_ID, interview_at="2026-12-01T10:30",
                                     follow_up_at="2026-12-05", notes="Recruiter call went well.")
                submitted_id = a3["id"]
            else:
                print("NOTE approval gate blocked the post-submit scenario:", gate)
            variant = db.one("SELECT id FROM resume_versions WHERE kind='variant' ORDER BY id DESC LIMIT 1")
            letter = db.one("SELECT id FROM cover_letters ORDER BY id DESC LIMIT 1")
            # a run row so /runs and /runs/<id> render
            run_id = "00000000-0000-4000-8000-000000000001"
            with db.transaction():
                db.run("INSERT INTO runs(id, kind, trigger, status, started_at, finished_at, config_json, sources_json, "
                       "stats_json, errors_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (run_id, "agent", "manual", "partial", iso(), iso(), '{"app": {"database": "sqlite"}}',
                        '[{"source": "greenhouse", "board": "acme", "jobs": 3, "complete": true, "error": null},'
                        ' {"source": "uk_sponsor_register", "refreshed": false, "age_days": 1.2}]',
                        '{"new_jobs": 3, "duplicates": 0, "scored": 3, "prepared": 1, "closed": 0, "rate_limits": []}',
                        '["lever/thames: timeout"]'))
                for jid in ids:
                    db.run("INSERT INTO run_decisions(run_id, job_id, decision, reason, at) VALUES (?,?,?,?,?)",
                           (run_id, jid, "new", "first seen", iso()))
        finally:
            db.close()

        # ---------------- populated pages
        get(client, "/", "overview")
        get(client, "/jobs", "jobs")
        get(client, f"/jobs?job={ids[1]}", "jobs?job=<id>")
        get(client, "/jobs?q=engineer&country=IE&country=DE&experience=mid&remote=onsite&sponsorship=confirmed"
                    "&visa_friendly=1&min_score=40&rec=apply&status=discovered&bookmarked=1&include_closed=1"
                    "&has_salary=1&salary_min=1000&date_posted=week&sort=recent&company=acme", "jobs (all filters)")
        get(client, "/jobs?per_page=10&page=2", "jobs (page 2)")
        for jid in ids:
            get(client, f"/jobs/{jid}", f"job page {jid}")
        get(client, f"/jobs/compare?id={ids[0]}&id={ids[1]}&id={ids[2]}", "compare")
        get(client, "/applications", "applications")
        get(client, "/applications?status=awaiting_approval", "applications (filtered)")
        get(client, f"/applications/{app1['id']}", "application (awaiting approval)")
        if submitted_id:
            get(client, f"/applications/{submitted_id}", "application (interview)")
            get(client, f"/applications/{submitted_id}?opened=1", "application (opened)")
        get(client, "/sponsorship", "sponsorship")
        get(client, "/prep", "prep")
        get(client, "/studio", "studio (canonical)")
        if variant:
            get(client, f"/studio?v={variant['id']}", "studio (variant)")
        if letter:
            get(client, f"/letters/{letter['id']}", "letter")
        get(client, "/runs", "runs")
        get(client, f"/runs/{run_id}", "run detail")
        get(client, "/audit", "audit")
        get(client, "/audit?entity=application", "audit (filtered)")
        get(client, "/settings", "settings")
        get(client, "/static/app.css", "app.css")
        get(client, "/static/app.js", "app.js")

        # ---------------- administration + a second, normal user
        sdb = Database(cfg.database_url)
        try:
            users_mod.change_status(sdb, user2_id, "approve", f"user:{USER}", actor_id=ADMIN_ID)
        finally:
            sdb.close()
        get(client, "/admin/users", "admin users")
        get(client, "/audit?entity=admin", "audit (admin)")
        with TestClient(app) as c2:
            html = get(c2, "/login", "login page (second user)")
            r = c2.post("/login", data={"pre_csrf": pre_csrf(html), "username": USER2, "password": PASSWORD,
                                        "next": "/"}, follow_redirects=False)
            assert r.status_code == 303, f"second user login failed: {r.status_code} {r.text[:300]}"
            for url in ("/", "/jobs", f"/jobs/{ids[0]}", "/applications", "/studio", "/prep", "/sponsorship", "/mnc",
                        "/runs", "/audit", "/settings"):
                get(c2, url, f"user2 {url}")
            get(c2, "/admin/users", "user2 admin page", expect=404)
            get(c2, f"/applications/{app1['id']}", "user2 admin's application", expect=404)
            if letter:
                get(c2, f"/letters/{letter['id']}", "user2 admin's letter", expect=404)

    if FAILURES:
        print("\nFAILURES:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("\nAll pages rendered OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
