"""Browser layout check: seeds a throwaway SQLite DB, serves the dashboard and opens every main page at several
viewport widths in Chromium. Fails on horizontal page overflow, JavaScript errors, the job list overlapping the
detail pane, and card content spilling outside its card.

    .venv\\Scripts\\python.exe scripts\\ui_check.py [--shots DIR]
"""
from __future__ import annotations

import os
import argparse
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from jobagent import agent  # noqa: E402
from jobagent import settings as settings_mod  # noqa: E402
from jobagent.config import load_config  # noqa: E402
from jobagent.db import Database, iso  # noqa: E402
from jobagent.ingest import upsert_raw  # noqa: E402
from jobagent.matching import candidate_profile  # noqa: E402
from jobagent.resume import store as resume_store  # noqa: E402
from jobagent.sources import RawJob  # noqa: E402
from jobagent.web.app import create_app  # noqa: E402

RESUME = Path(os.environ.get("JOBAGENT_TEST_RESUME")
             or Path.home() / "OneDrive" / "Desktop" / "hrithik" / "Hrithik_Dwivedi_Resume_India.pdf")
USER, PASSWORD = "ui.check", "Ui-Check-Passw0rd!x"
PORT = 8797
WIDTHS = [390, 768, 1024, 1280, 1536]
JOBS = [
    ("Graduate Applied AI Engineer", "Resonance", "London, United Kingdom",
     "Python, PyTorch and LLM tooling. Graduate role. Visa sponsorship available.\nContact: talent@resonance.example"),
    ("Custom Software Engineering Associate", "Accenture", "Mumbai, India", "Java, .NET, SQL. 0-2 years."),
    ("IN_Associate_ AI Engineer_GCC_Advisory_Bangalore_Supercalifragilistic_Long_Token", "PwC",
     "Bengaluru, India", "Python, Azure OpenAI, LangChain. Recruiter: Priya Sharma (priya.sharma@pwc.example)."),
    ("Backend Engineer (Python) — Payments Platform, Senior-ish but open to strong mid-level candidates",
     "Thames Data Ltd", "London, United Kingdom", "Python, FastAPI, SQL, Docker. We can sponsor Skilled Worker visas."),
    (".NET Software Engineer", "Acme Payments Ltd", "Dublin, Ireland",
     "C#, .NET, ASP.NET Core, SQL on AWS. 2+ years. Visa sponsorship is available. careers@acme.example"),
    ("Platform Engineer", "Northwind Cloud", "Toronto, Canada", "Go, Kubernetes, Terraform. 3+ years."),
    # long multi-location strings + long employer names (these once pushed cards under the detail pane)
    ("Software Engineer", "PricewaterhouseCoopers Service Delivery Center (Bangalore) Private Limited",
     "Bengaluru, Karnataka, India; Hyderabad, Telangana, India; Pune, Maharashtra, India; Gurugram, Haryana, India",
     "Python, SQL, cloud. 1-3 years."),
]
PAGES = ["/", "/jobs", "/applications", "/mnc", "/sponsorship", "/prep", "/runs", "/audit", "/settings",
         "/recruiters", "/answers", "/logout", "/support", "/support?q=auto+apply+fill+form+manually",
         "/support/1", "/companion", "/admin/users", "/admin/support", "/admin/support/1",
         "/jobs?view=all", "/applications/1", "/welcome/2", "/welcome/3", "/welcome/5", "/admin/users?segment=healthcare"]
NEW_USER = "nina.new"
FASHION_USER = "diya.fashion"
USER_PAGES = ["/", "/mnc", "/sponsorship", "/prep", "/jobs", "/applications"]
WIZARD = ["/welcome/1", "/welcome/2", "/welcome/3", "/welcome/4", "/welcome/5"]
PUBLIC_PAGES = ["/login", "/signup", "/signed-out"]

LAYOUT_JS = r"""() => {
  const issues = [];
  const de = document.documentElement;
  if (de.scrollWidth > window.innerWidth + 1) {
    const culprits = [];
    document.querySelectorAll('body *').forEach((e) => {
      const r = e.getBoundingClientRect();
      if (r.right > window.innerWidth + 1 && !e.closest('.table-wrap, .sidebar, .pill-panel')) {
        const p = e.parentElement && e.parentElement.getBoundingClientRect();
        if (!p || p.right <= window.innerWidth + 1) culprits.push(`${e.tagName.toLowerCase()}.${String(e.className.baseVal ?? e.className).split(' ')[0]}`);
      }
    });
    issues.push(`page scrolls horizontally (${de.scrollWidth}px > ${window.innerWidth}px) caused by: ${culprits.slice(0, 4).join(', ')}`);
  }
  const pane = document.querySelector('.detail-pane');
  const vis = (el) => { const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden'; };
  if (pane && vis(pane)) {
    const p = pane.getBoundingClientRect();
    document.querySelectorAll('.job-card').forEach((c, i) => {
      const r = c.getBoundingClientRect();
      if (r.right > p.left + 1 && r.left < p.right && r.bottom > p.top && r.top < p.bottom)
        issues.push(`job card #${i + 1} overlaps the detail pane (${Math.round(r.right)} > ${Math.round(p.left)})`);
    });
  }
  document.querySelectorAll('.job-card, .card, .kpi, .app-card, .queue-card').forEach((c, i) => {
    if (!vis(c)) return;
    const r = c.getBoundingClientRect();
    if (r.width === 0) return;
    c.querySelectorAll('*').forEach((k) => {
      if (!vis(k) || k.closest('.table-wrap, .pill-panel, pre, .description, .kanban, .diff, details:not([open])')) return;
      const q = k.getBoundingClientRect();
      if (q.width && q.right > r.right + 2) {
        const label = (k.className && k.className.baseVal === undefined ? k.className : k.tagName) + '';
        issues.push(`${c.className.split(' ')[0]} #${i + 1}: "${label.slice(0, 40)}" overflows by ${Math.round(q.right - r.right)}px`);
      }
    });
  });
  return Array.from(new Set(issues)).slice(0, 12);
}"""


def seed(cfg) -> None:
    app = create_app(cfg)
    with TestClient(app) as c:
        html = c.get("/setup").text
        tok = re.search(r'name="pre_csrf" value="([^"]+)"', html).group(1)
        r = c.post("/setup", data={"pre_csrf": tok, "username": USER, "password": PASSWORD, "password2": PASSWORD},
                   follow_redirects=False)
        assert r.status_code == 303, r.text[:300]
    db = Database(cfg.database_url)
    try:
        s = settings_mod.get_all(db, 1)
        ids = []
        for n, (title, comp, loc, desc) in enumerate(JOBS):
            raw = RawJob(source="greenhouse", source_job_id=f"ui-{n}", title=title, company=comp, location_raw=loc,
                         url=f"https://boards.greenhouse.io/x/jobs/{n}", apply_url=f"https://boards.greenhouse.io/x/jobs/{n}",
                         description_text=desc, published_at=iso(), employment_type="full_time")
            _, jid, _ = upsert_raw(db, raw, s)
            if jid:
                ids.append(jid)
        with db.transaction():
            db.run("UPDATE jobs SET is_sample=0, last_verified_at=?", (iso(),))
        if RESUME.exists():
            resume = resume_store.import_original(db, str(RESUME), user_id=1)
            profile = candidate_profile(resume["structured"], resume["analysis"], s["candidate"])
            pv = agent.profile_version(resume, s)
            for jid in ids:
                agent.score_and_store(db, jid, profile, s, pv, user_id=1)
            for jid in ids[:3]:
                agent.prepare_application(db, jid, f"user:{USER}", profile, s, None, user_id=1)
        from jobagent import helpdesk
        from jobagent import users as users_mod
        from jobagent.web import security
        careers = {"priya.s": {"industries": ["healthcare", "education"], "roles": ["nurse", "teacher"],
                               "experience": "mid"},
                   "rahul.k": {"industries": ["finance"], "roles": ["accountant", "financial_analyst"],
                               "custom_roles": ["Tax Associate"], "experience": "entry"}}
        for uname, full, grant in (("priya.s", "Priya Sharma", True), ("rahul.k", "Rahul Kumar", False)):
            u = security.create_user(db, uname, PASSWORD, actor="ui", email=f"{uname}@example.com", full_name=full,
                                     role="user", status="approved")
            settings_mod.set_value(db, "career", careers[uname], "ui", user_id=u)
            if grant:
                users_mod.set_ai_access(db, u, True, "ui", monthly_limit_usd=2.5)
        security.create_user(db, NEW_USER, PASSWORD, actor="ui", email="nina@example.com", full_name="Nina New",
                             role="user", status="approved", needs_onboarding=True)
        # a regular (non-admin) fashion user: personal home, industry Top MNCs, skills & courses
        fu = security.create_user(db, FASHION_USER, PASSWORD, actor="ui", email="diya@example.com",
                                  full_name="Diya Example", role="user", status="approved")
        settings_mod.set_value(db, "career", {"industries": ["fashion"], "roles": ["fashion_stylist",
                                              "visual_merchandiser"], "experience": "entry"}, "ui", user_id=fu)
        settings_mod.set_value(db, "target_countries", {"tier1": ["IN"], "tier2": [], "tier3": []}, "ui", user_id=fu)
        for n, (title, comp) in enumerate((("Fashion Stylist", "Zara"), ("Visual Merchandiser", "H&M"),
                                           ("Junior Fashion Stylist for Celebrity Shoots and Editorial Campaigns",
                                            "Aditya Birla Fashion & Retail"))):
            raw = RawJob(source="greenhouse", source_job_id=f"ui-f{n}", title=title, company=comp,
                         location_raw="Mumbai, India", url=f"https://boards.greenhouse.io/f/jobs/{n}",
                         apply_url=f"https://boards.greenhouse.io/f/jobs/{n}", description_text="Styling role.",
                         published_at=iso(), employment_type="full_time")
            upsert_raw(db, raw, s)
        with db.transaction():
            db.run("UPDATE jobs SET is_sample=0, last_verified_at=?", (iso(),))
        # the admin's own preferences (multi-industry) so /welcome/3 shows several groups
        settings_mod.set_value(db, "career", {"industries": ["technology", "business"],
                                              "roles": ["software_engineer", "business_analyst"],
                                              "experience": "entry"}, "ui", user_id=1)
        tid = helpdesk.create_ticket(db, user_id=1, category="companion",
                                     subject="Workday form did not fill the work authorisation question",
                                     body="I opened a Workday application and pressed Fill this form. Most fields "
                                          "filled but the work authorisation dropdown stayed empty.", actor="ui")
        helpdesk.reply(db, tid, author_id=1, staff=True, actor="ui",
                       body="Workday loads that dropdown late. Press Fill this form again once the page settles.")
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="save a screenshot of every page/width into this folder")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ja-ui-")
    cfg = load_config()
    cfg.database_url = f"sqlite:///{Path(tmp, 't.db').as_posix()}"
    cfg.secret_key = "u" * 40
    cfg.resend_api_key, cfg.smtp_host = None, None
    cfg.public_url = f"http://127.0.0.1:{PORT}"
    seed(cfg)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)
    base = f"http://127.0.0.1:{PORT}"
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width in WIDTHS:
            ctx = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"JS error: {e}"))
            page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
            for path in PUBLIC_PAGES:
                errors.clear()
                resp = page.goto(base + path)
                page.wait_for_load_state("networkidle")
                if resp is None or resp.status >= 400:
                    failures.append(f"[{width}px] {path}: HTTP {resp.status if resp else '?'}")
                    continue
                for i in page.evaluate(LAYOUT_JS) + errors:
                    failures.append(f"[{width}px] {path}: {i}")
                if shots:
                    page.screenshot(path=str(shots / f"{path.strip('/').replace('-', '_')}_{width}.png"))
            page.goto(base + "/login")
            page.fill("input[name=username]", USER)
            page.fill("input[name=password]", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            jobs_with_pane = ["/jobs?job=1", "/jobs?job=3"]
            for path in PAGES + jobs_with_pane:
                errors.clear()
                resp = page.goto(base + path)
                page.wait_for_load_state("networkidle")
                if resp is None or resp.status >= 400:
                    failures.append(f"[{width}px] {path}: HTTP {resp.status if resp else '?'}")
                    continue
                issues = page.evaluate(LAYOUT_JS) + errors
                for i in issues:
                    failures.append(f"[{width}px] {path}: {i}")
                if shots:
                    name = re.sub(r"[^a-z0-9]+", "_", path.strip("/") or "overview")
                    page.screenshot(path=str(shots / f"{name}_{width}.png"), full_page=False)
            # a regular user: personal home (with the "what's new" pop-up), industry pages
            page.context.clear_cookies()
            page.goto(base + "/login")
            page.fill("input[name=username]", FASHION_USER)
            page.fill("input[name=password]", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            for path in USER_PAGES:
                errors.clear()
                resp = page.goto(base + path)
                page.wait_for_load_state("networkidle")
                if resp is None or resp.status >= 400:
                    failures.append(f"[{width}px] {path} (user): HTTP {resp.status if resp else '?'}")
                    continue
                for i in page.evaluate(LAYOUT_JS) + errors:
                    failures.append(f"[{width}px] {path} (user): {i}")
                if shots:
                    name = re.sub(r"[^a-z0-9]+", "_", path.strip("/") or "home")
                    page.screenshot(path=str(shots / f"user_{name}_{width}.png"), full_page=False)
            # a brand-new account: the onboarding wizard in focus mode (no sidebar)
            page.context.clear_cookies()
            page.goto(base + "/login")
            page.fill("input[name=username]", NEW_USER)
            page.fill("input[name=password]", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            if "/welcome" not in page.url:
                failures.append(f"[{width}px] new account was not sent to onboarding (at {page.url})")
            for path in WIZARD:
                errors.clear()
                resp = page.goto(base + path)
                page.wait_for_load_state("networkidle")
                if resp is None or resp.status >= 400:
                    failures.append(f"[{width}px] {path} (new user): HTTP {resp.status if resp else '?'}")
                    continue
                if "/welcome" not in page.url:
                    failures.append(f"[{width}px] {path} (new user): ended up at {page.url}")
                for i in page.evaluate(LAYOUT_JS) + errors:
                    failures.append(f"[{width}px] {path} (new user): {i}")
                if shots:
                    page.screenshot(path=str(shots / f"new_{path.strip('/').replace('/', '_')}_{width}.png"),
                                    full_page=True)
                if path == "/welcome/2":
                    page.click("input[value=healthcare]", force=True)
                    page.click("form[action=\"/welcome/2\"] button[type=submit]")
                    page.wait_for_load_state("networkidle")
            ctx.close()
        browser.close()
    server.should_exit = True
    if failures:
        print("\n".join(failures))
        print(f"\n{len(failures)} UI issue(s).")
        return 1
    n = len(PAGES) + len(PUBLIC_PAGES) + len(WIZARD) + len(USER_PAGES) + 2
    print(f"UI check passed: {n} pages x {len(WIDTHS)} widths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
