"""Direct SQL seeding of SYNTHETIC dashboard data, plus an HTML accessibility checker."""
from __future__ import annotations

import uuid
from datetime import timedelta
from html.parser import HTMLParser

from jobagent.db import Database, iso, utcnow
from jobagent.normalize import normalize_employer, normalize_title


def employer(db: Database, name: str) -> int:
    norm = normalize_employer(name) + "-" + uuid.uuid4().hex[:6] if not name else normalize_employer(name)
    r = db.one("SELECT id FROM employers WHERE normalized_name=?", (norm,))
    if r:
        return r["id"]
    return db.insert("INSERT INTO employers(name, normalized_name, created_at) VALUES (?,?,?)", (name, norm, iso()))


def insert_job(db: Database, title="Software Engineer", company="Acme", *, country="IE", city="Dublin",
               remote_type="onsite", employment_type="full_time", seniority="junior", salary_min=None,
               salary_max=None, salary_currency=None, published_hours_ago=1.0, status="open", bookmarked=0,
               description="C# and .NET", apply_url=None, score=None, rec=None, sponsorship=None,
               app_status=None, location_raw=None, user_id=1) -> int:
    """Shared job + (optionally) the given user's score, bookmark and application (default: the admin, id 1)."""
    emp = employer(db, company)
    now = utcnow()
    pub = iso(now - timedelta(hours=published_hours_ago)) if published_hours_ago is not None else None
    jid = db.insert(
        "INSERT INTO jobs(job_key, employer_id, title, normalized_title, location_raw, country, city, remote_type, "
        "employment_type, seniority, description_text, salary_min, salary_max, salary_currency, salary_period, "
        "published_at, first_discovered_at, last_seen_at, last_verified_at, status, apply_url, canonical_url)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, emp, title, normalize_title(title), location_raw, country, city, remote_type,
         employment_type, seniority, description, salary_min, salary_max, salary_currency,
         "year" if salary_max else None, pub, iso(now - timedelta(hours=published_hours_ago or 0)), iso(now), iso(now),
         status, apply_url or f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().int % 10**8}", None))
    if bookmarked:
        db.run("INSERT INTO user_bookmarks(user_id, job_id, created_at) VALUES (?,?,?)", (user_id, jid, iso()))
    if score is not None:
        db.run("INSERT INTO job_scores(job_id, user_id, score, recommendation, breakdown_json, details_json, weights_json, "
               "created_at) VALUES (?,?,?,?,'{}','{}','{}',?)", (jid, user_id, score, rec or "review", iso()))
    if sponsorship:
        db.run("INSERT INTO sponsorship_assessments(job_id, classification, evidence_json, method, assessed_at) "
               "VALUES (?,?,'[]','rules',?)", (jid, sponsorship, iso()))
    if app_status:
        db.run("INSERT INTO applications(job_id, user_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
               (jid, user_id, app_status, iso(), iso()))
    return jid


class LabelChecker(HTMLParser):
    """Finds form controls without an associated label / aria-label."""

    SKIP_TYPES = {"hidden", "submit", "button", "reset", "image"}

    def __init__(self):
        super().__init__()
        self.label_depth = 0
        self.label_for = set()
        self.controls = []  # (tag, attrs, inside_label)
        self.html_lang = None
        self.skip_link = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html_lang = a.get("lang")
        if tag == "a" and (a.get("href") or "").startswith("#") and "skip" in (a.get("class") or ""):
            self.skip_link = True
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            if tag == "input" and (a.get("type") or "text").lower() in self.SKIP_TYPES:
                return
            self.controls.append((tag, a, self.label_depth > 0))

    def handle_endtag(self, tag):
        if tag == "label" and self.label_depth:
            self.label_depth -= 1

    def unlabeled(self):
        bad = []
        for tag, a, inside in self.controls:
            if inside or a.get("aria-label") or a.get("aria-labelledby") or a.get("title"):
                continue
            if a.get("id") and a["id"] in self.label_for:
                continue
            bad.append(f"<{tag} name={a.get('name')!r} id={a.get('id')!r}>")
        return bad
