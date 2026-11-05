"""Read models for the dashboard: LinkedIn-style job search, overview statistics, tracker lists."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from ..db import Database, iso, loads, utcnow
from ..mnc import catalog as mnc_catalog

# The user's own latest score (one ? parameter: user_id). Jobs are shared; scores are per user.
LATEST_SCORE = "LEFT JOIN job_scores sc ON sc.id = (SELECT MAX(id) FROM job_scores WHERE job_id = j.id AND user_id = ?)"
USER_APP = "LEFT JOIN applications a ON a.job_id = j.id AND a.user_id = ?"
USER_BOOKMARK = "LEFT JOIN user_bookmarks ub ON ub.job_id = j.id AND ub.user_id = ?"
LATEST_SPONSOR = "LEFT JOIN sponsorship_assessments sa ON sa.id = (SELECT MAX(id) FROM sponsorship_assessments WHERE job_id = j.id)"

EXPERIENCE_LEVELS = {  # LinkedIn-style label -> seniority values
    "internship": ["intern"], "entry": ["junior"], "not_specified": ["unspecified"], "mid": ["mid"],
    "senior": ["senior", "staff", "lead", "principal"],
}
EXPERIENCE_LABELS = {"internship": "Internship", "entry": "Entry level / Junior", "not_specified": "Not specified",
                     "mid": "Mid level", "senior": "Senior & above"}
JOB_TYPES = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "temporary": "Temporary",
             "internship": "Internship", "unspecified": "Not specified"}
REMOTE_TYPES = {"onsite": "On-site", "hybrid": "Hybrid", "remote": "Remote", "unknown": "Not specified"}
DATE_POSTED = {"any": "Any time", "24h": "Past 24 hours", "3d": "Past 3 days", "week": "Past week", "month": "Past month"}
SPONSORSHIP = {"confirmed": "Confirmed", "likely": "Likely", "unknown": "Unknown", "not_supported": "Not supported"}
RECOMMENDATIONS = {"apply": "Apply", "review": "Review", "improve_skills": "Improve skills", "skip": "Skip"}
SORTS = {"relevant": "Most relevant", "recent": "Most recent", "salary": "Highest salary", "company": "Company A-Z"}
APP_STATUSES = ["discovered", "shortlisted", "awaiting_approval", "ready_to_apply", "application_started", "submitted",
                "submission_uncertain", "submission_failed", "interview", "assessment", "offer", "rejected",
                "withdrawn", "closed"]


@dataclass
class JobFilters:
    q: str = ""
    location: str = ""
    countries: list[str] = field(default_factory=list)
    date_posted: str = "any"
    experience: list[str] = field(default_factory=list)
    job_type: list[str] = field(default_factory=list)
    remote: list[str] = field(default_factory=list)
    sponsorship: list[str] = field(default_factory=list)
    recommendation: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    company: str = ""
    salary_min: int = 0
    has_salary: bool = False
    min_score: int = 0
    bookmarked: bool = False
    include_closed: bool = False
    visa_friendly: bool = False
    mnc: list[str] = field(default_factory=list)   # ["all"] or catalogue keys
    sort: str = "relevant"
    page: int = 1
    per_page: int = 25

    @classmethod
    def from_query(cls, qp) -> "JobFilters":
        def lst(name, allowed):
            vals = []
            for v in qp.getlist(name):
                for part in str(v).split(","):
                    part = part.strip()
                    if part in allowed and part not in vals:
                        vals.append(part)
            return vals

        def num(name, lo, hi, default):
            try:
                return max(lo, min(hi, int(qp.get(name) or default)))
            except ValueError:
                return default
        from ..settings import COUNTRY_NAMES
        return cls(
            q=(qp.get("q") or "").strip()[:100],
            location=(qp.get("location") or "").strip()[:60],
            countries=lst("country", COUNTRY_NAMES),
            date_posted=qp.get("date_posted") if qp.get("date_posted") in DATE_POSTED else "any",
            experience=lst("experience", EXPERIENCE_LEVELS),
            job_type=lst("job_type", JOB_TYPES),
            remote=lst("remote", REMOTE_TYPES),
            sponsorship=lst("sponsorship", SPONSORSHIP),
            recommendation=lst("rec", RECOMMENDATIONS),
            status=lst("status", APP_STATUSES),
            company=(qp.get("company") or "").strip()[:80],
            salary_min=num("salary_min", 0, 1_000_000, 0),
            has_salary=qp.get("has_salary") == "1",
            min_score=num("min_score", 0, 100, 0),
            bookmarked=qp.get("bookmarked") == "1",
            include_closed=qp.get("include_closed") == "1",
            visa_friendly=qp.get("visa_friendly") == "1",
            mnc=lst("mnc", {"all"} | {c["key"] for c in mnc_catalog()}),
            sort=qp.get("sort") if qp.get("sort") in SORTS else "relevant",
            page=num("page", 1, 10_000, 1),
            per_page=num("per_page", 10, 100, 25),
        )

    def active_count(self) -> int:
        return sum(bool(x) for x in (self.countries, self.date_posted != "any", self.experience, self.job_type,
                                     self.remote, self.sponsorship, self.recommendation, self.status, self.company,
                                     self.salary_min or self.has_salary, self.min_score, self.bookmarked,
                                     self.include_closed, self.visa_friendly, self.mnc))


def _like(s: str) -> str:
    return "%" + s.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def search_jobs(db: Database, f: JobFilters, include_sample: bool = False, *, user_id: int) -> tuple[list[dict], int]:
    """Shared job list joined with THIS user's latest score, application and bookmark."""
    where, params = [], []
    if not include_sample:
        where.append("j.is_sample = 0")
    if not f.include_closed:
        where.append("j.status = 'open'")
    if f.q:
        where.append("(LOWER(j.title) LIKE ? ESCAPE '!' OR LOWER(e.name) LIKE ? ESCAPE '!' OR "
                     "LOWER(j.description_text) LIKE ? ESCAPE '!')")
        params += [_like(f.q)] * 3
    if f.location:
        where.append("(LOWER(COALESCE(j.location_raw,'')) LIKE ? ESCAPE '!' OR LOWER(COALESCE(j.city,'')) LIKE ? ESCAPE '!'"
                     " OR LOWER(COALESCE(j.country,'')) = ?)")
        params += [_like(f.location), _like(f.location), f.location.lower()]
    if f.countries:
        where.append("j.country IN (" + ",".join("?" * len(f.countries)) + ")")
        params += f.countries
    if f.date_posted != "any":
        delta = {"24h": timedelta(hours=24), "3d": timedelta(days=3), "week": timedelta(days=7),
                 "month": timedelta(days=30)}[f.date_posted]
        where.append("COALESCE(j.published_at, j.first_discovered_at) >= ?")
        params.append(iso(utcnow() - delta))
    if f.experience:
        levels = [lv for e in f.experience for lv in EXPERIENCE_LEVELS[e]]
        where.append("j.seniority IN (" + ",".join("?" * len(levels)) + ")")
        params += levels
    if f.job_type:
        where.append("COALESCE(j.employment_type,'unspecified') IN (" + ",".join("?" * len(f.job_type)) + ")")
        params += f.job_type
    if f.remote:
        where.append("j.remote_type IN (" + ",".join("?" * len(f.remote)) + ")")
        params += f.remote
    if f.sponsorship:
        where.append("COALESCE(sa.classification,'unknown') IN (" + ",".join("?" * len(f.sponsorship)) + ")")
        params += f.sponsorship
    if f.mnc:
        if "all" in f.mnc:
            where.append("e.mnc_key IS NOT NULL")
        else:
            where.append("e.mnc_key IN (" + ",".join("?" * len(f.mnc)) + ")")
            params += f.mnc
    if f.visa_friendly:
        where.append("COALESCE(sa.classification,'unknown') IN ('confirmed','likely')")
    if f.recommendation:
        where.append("sc.recommendation IN (" + ",".join("?" * len(f.recommendation)) + ")")
        params += f.recommendation
    if f.status:
        conds = []
        if "discovered" in f.status:
            conds.append("a.id IS NULL")
        others = [s for s in f.status if s != "discovered"]
        if others:
            conds.append("a.status IN (" + ",".join("?" * len(others)) + ")")
            params += others
        where.append("(" + " OR ".join(conds) + ")")
    if f.company:
        where.append("LOWER(e.name) LIKE ? ESCAPE '!'")
        params.append(_like(f.company))
    if f.has_salary:
        where.append("j.salary_max IS NOT NULL")
    if f.salary_min:
        where.append("j.salary_max >= ?")
        params.append(f.salary_min)
    if f.min_score:
        where.append("sc.score >= ?")
        params.append(f.min_score)
    if f.bookmarked:
        where.append("ub.job_id IS NOT NULL")
    order = {
        "relevant": "COALESCE(sc.score, 0) DESC, COALESCE(j.published_at, j.first_discovered_at) DESC",
        "recent": "COALESCE(j.published_at, j.first_discovered_at) DESC",
        "salary": "COALESCE(j.salary_max, 0) DESC, COALESCE(sc.score, 0) DESC",
        "company": "LOWER(e.name) ASC, j.title ASC",
    }[f.sort]
    base = (f"FROM jobs j JOIN employers e ON e.id = j.employer_id {LATEST_SCORE} {LATEST_SPONSOR} "
            f"{USER_APP} {USER_BOOKMARK}" + (" WHERE " + " AND ".join(where) if where else ""))
    params = [user_id, user_id, user_id] + params
    total = int(db.scalar("SELECT COUNT(*) " + base, params) or 0)
    rows = db.all(
        "SELECT j.id, j.title, j.country, j.city, j.location_raw, j.remote_type, j.employment_type, j.seniority, "
        "j.salary_min, j.salary_max, j.salary_currency, j.salary_period, j.experience_min_years, j.published_at, "
        "j.first_discovered_at, j.status AS job_status, j.apply_url, j.canonical_url, "
        "CASE WHEN ub.job_id IS NULL THEN 0 ELSE 1 END AS bookmarked, j.is_sample, "
        "e.name AS employer, e.id AS employer_id, e.mnc_key, e.sector, sc.score, sc.recommendation, "
        "sa.classification AS sponsorship, "
        "a.id AS app_id, a.status AS app_status " + base + f" ORDER BY {order} LIMIT ? OFFSET ?",
        params + [f.per_page, (f.page - 1) * f.per_page])
    return rows, total


def job_detail(db: Database, job_id: int, *, user_id: int) -> dict | None:
    """A shared job with THIS user's score, application, bookmark and prep plan (never anyone else's)."""
    job = db.one(f"SELECT j.*, e.name AS employer, e.uk_sponsor_status, e.uk_sponsor_routes, e.uk_sponsor_checked_at, "
                 f"sc.score, sc.recommendation, sc.details_json, sc.created_at AS scored_at, sa.classification AS "
                 f"sponsorship, sa.evidence_json, sa.assessed_at, a.id AS app_id, a.status AS app_status, "
                 f"CASE WHEN ub.job_id IS NULL THEN 0 ELSE 1 END AS user_bookmarked "
                 f"FROM jobs j JOIN employers e ON e.id=j.employer_id {LATEST_SCORE} {LATEST_SPONSOR} "
                 f"{USER_APP} {USER_BOOKMARK} WHERE j.id=?", (user_id, user_id, user_id, job_id))
    if not job:
        return None
    job["bookmarked"] = job.pop("user_bookmarked")
    job["details"] = loads(job.pop("details_json"), {}) or {}
    job["evidence"] = loads(job.pop("evidence_json"), []) or []
    job["languages"] = loads(job.get("languages_required"), []) or []
    job["listings"] = db.all("SELECT source, source_job_id, url, first_seen_at, last_seen_at FROM job_listings "
                             "WHERE job_id=? ORDER BY first_seen_at", (job_id,))
    plan = db.one("SELECT plan_json FROM prep_plans WHERE job_id=? AND user_id=?", (job_id, user_id))
    job["plan"] = loads(plan["plan_json"], {}) if plan else None
    job["similar_applications"] = db.all(
        "SELECT a.id, a.status, j2.title, a.submitted_at FROM applications a JOIN jobs j2 ON j2.id=a.job_id "
        "WHERE a.user_id=? AND j2.employer_id=? AND j2.id != ? ORDER BY a.id DESC LIMIT 5",
        (user_id, job["employer_id"], job_id))
    return job


def facet_counts(db: Database) -> dict:
    """Counts of open, non-sample jobs per facet value (shown next to LinkedIn-style filter options). Shared data."""
    out = {}
    for col, name in (("j.remote_type", "remote"), ("COALESCE(j.employment_type,'unspecified')", "job_type"),
                      ("j.seniority", "seniority"), ("j.country", "country"),
                      ("COALESCE(sa.classification,'unknown')", "sponsorship")):
        rows = db.all(f"SELECT {col} AS v, COUNT(*) AS n FROM jobs j {LATEST_SPONSOR} WHERE j.status='open' AND "
                      f"j.is_sample=0 GROUP BY {col}")
        out[name] = {r["v"]: r["n"] for r in rows if r["v"] is not None}
    exp = {}
    for key, levels in EXPERIENCE_LEVELS.items():
        exp[key] = sum(out["seniority"].get(lv, 0) for lv in levels)
    out["experience"] = exp
    return out


def overview(db: Database, *, user_id: int) -> dict:
    s = {}
    s["jobs_total"] = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE is_sample=0") or 0)
    s["jobs_open"] = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE is_sample=0 AND status='open'") or 0)
    last_run = db.one("SELECT * FROM runs WHERE kind='agent' AND status IN ('succeeded','partial') ORDER BY started_at "
                      "DESC LIMIT 1")
    s["last_run"] = last_run
    s["new_latest_run"] = int(db.scalar("SELECT COUNT(*) FROM run_decisions WHERE run_id=? AND decision='new' "
                                        "AND user_id IS NULL", (last_run["id"],)) or 0) if last_run else 0
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM applications WHERE is_sample=0 "
                                                  "AND user_id=? GROUP BY status", (user_id,))}
    s["app_counts"] = counts
    submitted_family = ["submitted", "interview", "assessment", "offer", "rejected"]
    reached = sum(counts.get(k, 0) for k in submitted_family)
    # 'withdrawn' after submission is counted from audit trail to keep denominators honest
    withdrawn_after = int(db.scalar(
        "SELECT COUNT(DISTINCT a.id) FROM applications a JOIN audit_events ev ON ev.entity='application' AND "
        "ev.entity_id = CAST(a.id AS TEXT) AND ev.to_status='submitted' WHERE a.status='withdrawn' AND a.is_sample=0 "
        "AND a.user_id=?", (user_id,)) or 0)
    s["submitted_total"] = reached + withdrawn_after
    s["awaiting_approval"] = counts.get("awaiting_approval", 0)
    s["in_progress"] = sum(counts.get(k, 0) for k in ("shortlisted", "ready_to_apply", "application_started"))
    s["interviews"] = counts.get("interview", 0) + counts.get("assessment", 0)
    s["offers"] = counts.get("offer", 0)
    s["rejections"] = counts.get("rejected", 0)
    s["problems"] = counts.get("submission_failed", 0) + counts.get("submission_uncertain", 0)
    s["sponsorship_confirmed"] = int(db.scalar(
        f"SELECT COUNT(*) FROM jobs j {LATEST_SPONSOR} WHERE j.status='open' AND j.is_sample=0 AND "
        f"sa.classification='confirmed'") or 0)
    avg = db.scalar(f"SELECT AVG(sc.score) FROM jobs j {LATEST_SCORE} WHERE j.status='open' AND j.is_sample=0 AND "
                    f"sc.score IS NOT NULL", (user_id,))
    s["avg_score"] = round(float(avg), 1) if avg is not None else None
    denom = s["submitted_total"]
    responded = s["interviews"] + s["offers"] + s["rejections"]
    s["metrics"] = [
        {"label": "Response rate", "num": responded, "den": denom,
         "definition": "(interview + assessment + offer + rejected) / applications submitted"},
        {"label": "Interview rate", "num": s["interviews"] + s["offers"], "den": denom,
         "definition": "(interview + assessment + offer) / applications submitted"},
        {"label": "Offer rate", "num": s["offers"], "den": denom, "definition": "offers / applications submitted"},
    ]
    today = utcnow().date().isoformat()
    s["upcoming"] = db.all(
        "SELECT a.id, j.title, e.name AS employer, a.interview_at, a.follow_up_at, j.deadline FROM applications a "
        "JOIN jobs j ON j.id=a.job_id JOIN employers e ON e.id=j.employer_id WHERE a.is_sample=0 AND a.user_id=? AND "
        "((a.interview_at >= ?) OR (a.follow_up_at >= ?) OR (j.deadline >= ?)) ORDER BY COALESCE(a.interview_at, "
        "a.follow_up_at, j.deadline) LIMIT 10", (user_id, today, today, today))
    weeks = []
    start = utcnow() - timedelta(weeks=11)
    for i in range(12):
        w0 = start + timedelta(weeks=i)
        w1 = w0 + timedelta(weeks=1)
        n = int(db.scalar("SELECT COUNT(*) FROM applications WHERE is_sample=0 AND user_id=? AND submitted_at >= ? "
                          "AND submitted_at < ?", (user_id, iso(w0), iso(w1))) or 0)
        weeks.append((w0.strftime("%d %b"), n))
    s["chart_weeks"] = weeks
    s["chart_countries"] = [(r["country"] or "?", r["n"]) for r in db.all(
        "SELECT country, COUNT(*) AS n FROM jobs WHERE status='open' AND is_sample=0 GROUP BY country ORDER BY n DESC "
        "LIMIT 10")]
    s["chart_roles"] = [(r["seniority"], r["n"]) for r in db.all(
        "SELECT seniority, COUNT(*) AS n FROM jobs WHERE status='open' AND is_sample=0 GROUP BY seniority ORDER BY n DESC")]
    s["chart_sponsorship"] = [(r["c"], r["n"]) for r in db.all(
        f"SELECT COALESCE(sa.classification,'unknown') AS c, COUNT(*) AS n FROM jobs j {LATEST_SPONSOR} "
        f"WHERE j.status='open' AND j.is_sample=0 GROUP BY COALESCE(sa.classification,'unknown') ORDER BY n DESC")]
    s["chart_outcomes"] = [(k, counts.get(k, 0)) for k in submitted_family + ["withdrawn"] if counts.get(k, 0)]
    s["has_sample"] = bool(db.scalar("SELECT COUNT(*) FROM jobs WHERE is_sample=1"))
    return s


def mnc_overview(db: Database, *, user_id: int) -> list[dict]:
    """Every catalogue company with its open software-job count, the user's best match and their applications."""
    stats = {r["mnc_key"]: r for r in db.all(
        f"SELECT e.mnc_key, COUNT(*) AS open_jobs, MAX(sc.score) AS best_score, "
        f"SUM(CASE WHEN sc.recommendation='apply' THEN 1 ELSE 0 END) AS apply_count, "
        f"SUM(CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END) AS applications "
        f"FROM jobs j JOIN employers e ON e.id=j.employer_id {LATEST_SCORE} {USER_APP} "
        f"WHERE j.status='open' AND j.is_sample=0 AND e.mnc_key IS NOT NULL GROUP BY e.mnc_key", (user_id, user_id))}
    out = []
    for c in mnc_catalog():
        st = stats.get(c["key"], {})
        feed = (c.get("feed") or {}).get("type")
        out.append({**c, "open_jobs": int(st.get("open_jobs") or 0), "best_score": st.get("best_score"),
                    "apply_count": int(st.get("apply_count") or 0), "applications": int(st.get("applications") or 0),
                    "has_feed": bool(feed)})
    out.sort(key=lambda c: (-c["open_jobs"], c["name"]))
    return out


def applications_list(db: Database, status: list[str] | None = None, *, user_id: int) -> list[dict]:
    where, params = ["a.is_sample = 0", "a.user_id = ?"], [user_id]
    if status:
        where.append("a.status IN (" + ",".join("?" * len(status)) + ")")
        params += status
    return db.all(
        f"SELECT a.*, j.title, j.country, j.city, j.status AS job_status, j.apply_url, e.name AS employer, sc.score, "
        f"sa.classification AS sponsorship FROM applications a JOIN jobs j ON j.id=a.job_id JOIN employers e ON "
        f"e.id=j.employer_id {LATEST_SCORE} {LATEST_SPONSOR} WHERE " + " AND ".join(where) +
        " ORDER BY a.updated_at DESC", [user_id] + params)


def toggle_bookmark(db: Database, job_id: int, *, user_id: int) -> bool:
    """Bookmark / un-bookmark a shared job for this user. Returns the new state."""
    with db.transaction():
        if not db.one("SELECT id FROM jobs WHERE id=?", (job_id,)):
            raise LookupError("Job not found")
        if db.one("SELECT 1 AS x FROM user_bookmarks WHERE user_id=? AND job_id=?", (user_id, job_id)):
            db.run("DELETE FROM user_bookmarks WHERE user_id=? AND job_id=?", (user_id, job_id))
            return False
        db.run("INSERT INTO user_bookmarks(user_id, job_id, created_at) VALUES (?,?,?)", (user_id, job_id, iso()))
        return True


# ---------------------------------------------------------------- per-user visibility of shared records

def visible_audit(db: Database, *, user_id: int, username: str, is_admin: bool, entity: str = "",
                  limit: int = 300) -> list[dict]:
    """Audit events this user may see. Everyone sees events about their own applications/resumes/settings and
    their own actions; the admin additionally sees account administration, auth and run events, but NOT the
    contents of other users' job hunts."""
    me = f"user:{username}"
    own_apps = {str(r["id"]) for r in db.all("SELECT id FROM applications WHERE user_id=?", (user_id,))}
    own_res = {str(r["id"]) for r in db.all("SELECT id FROM resume_versions WHERE user_id=?", (user_id,))}
    params, where = [], ""
    if entity:
        where, params = "WHERE entity=?", [entity]
    out = []
    for r in db.all(f"SELECT * FROM audit_events {where} ORDER BY id DESC LIMIT 3000", params):
        detail = loads(r["detail_json"], {}) or {}
        ent, actor = r["entity"], r["actor"] or ""
        if ent == "application":
            ok = r["entity_id"] in own_apps
        elif ent == "resume":
            ok = r["entity_id"] in own_res
        elif ent == "settings":
            ok = actor == me or (is_admin and "user_id" not in detail)
        elif ent in ("auth", "admin"):
            ok = is_admin or actor == me or r["entity_id"] == str(user_id)
        elif ent == "run":
            ok = is_admin or actor == me
        elif ent == "job":
            ok = actor == me or (is_admin and detail.get("user_id") in (None, user_id))
        else:
            ok = is_admin
        if ok:
            out.append(r)
            if len(out) >= limit:
                break
    return out


def visible_runs(db: Database, *, user_id: int, is_admin: bool, limit: int = 100) -> list[dict]:
    if is_admin:
        return db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
    return db.all("SELECT * FROM runs WHERE user_id=? ORDER BY started_at DESC LIMIT ?", (user_id, limit))
