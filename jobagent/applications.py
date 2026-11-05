"""Application lifecycle with a deterministic state machine and job-specific, fingerprinted approvals.

Safety rules enforced here (not in the UI):
* one application per job identity (DB unique constraint) and no second application to an equivalent vacancy
* final submission requires an unexpired, unconsumed approval whose fingerprint matches the CURRENT employer,
  role, destination URL, resume version/content, cover letter and answers
* approvals are single-use (conditional UPDATE, safe under concurrency)
* 'submitted' requires evidence; timeouts become 'submission_uncertain' and are never auto-retried
* AI output can never call these functions on its own; only user actions and the deterministic agent can."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from urllib.parse import urlsplit

from . import settings as settings_mod
from .db import Database, IntegrityError, audit, dumps, iso, loads, parse_iso, utcnow
from .normalize import canonical_url
from .settings import COUNTRY_NAMES

PRE_SUBMIT = {"shortlisted", "awaiting_approval", "ready_to_apply"}
POST_SUBMIT = {"submitted", "interview", "assessment", "offer", "rejected"}
TRANSITIONS: dict[str, set[str]] = {
    "shortlisted": {"awaiting_approval", "withdrawn", "closed"},
    "awaiting_approval": {"ready_to_apply", "shortlisted", "withdrawn", "closed"},
    "ready_to_apply": {"application_started", "awaiting_approval", "withdrawn", "closed"},
    "application_started": {"submitted", "submission_uncertain", "submission_failed"},
    "submission_uncertain": {"submitted", "submission_failed"},
    "submission_failed": {"awaiting_approval", "withdrawn", "closed"},
    "submitted": {"interview", "assessment", "rejected", "offer", "withdrawn"},
    "interview": {"interview", "assessment", "offer", "rejected", "withdrawn"},
    "assessment": {"interview", "offer", "rejected", "withdrawn"},
    "offer": {"withdrawn", "rejected"},
    "rejected": set(),
    "withdrawn": set(),
    "closed": {"shortlisted"},
}
STATUS_LABELS = {
    "discovered": "Discovered", "shortlisted": "Shortlisted", "awaiting_approval": "Awaiting approval",
    "ready_to_apply": "Ready to apply", "application_started": "Application started", "submitted": "Submitted",
    "submission_uncertain": "Submission uncertain", "submission_failed": "Submission failed",
    "interview": "Interview", "assessment": "Assessment", "offer": "Offer", "rejected": "Rejected",
    "withdrawn": "Withdrawn", "closed": "Closed",
}
TRUSTED_ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "arbeitnow.com", "workable.com",
                     "smartrecruiters.com", "myworkdayjobs.com", "personio.de", "personio.com", "recruitee.com",
                     "teamtailor.com", "bamboohr.com", "join.com")


class WorkflowError(Exception):
    """A refused action. `code` is stable for tests/UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _get_app(db: Database, app_id: int, user_id: int | None) -> dict:
    """The application, which must belong to `user_id`. Only system jobs (sweeps, closures) pass None."""
    if user_id is None:
        a = db.one("SELECT * FROM applications WHERE id=?", (app_id,))
    else:
        a = db.one("SELECT * FROM applications WHERE id=? AND user_id=?", (app_id, user_id))
    if not a:
        raise WorkflowError("not_found", "Application not found.")
    return a


def get_app(db: Database, app_id: int, *, user_id: int) -> dict | None:
    """Ownership-checked read for the web layer (None -> 404)."""
    return db.one("SELECT * FROM applications WHERE id=? AND user_id=?", (app_id, user_id))


def _transition(db: Database, app: dict, to: str, actor: str, reason: str, extra: dict | None = None,
                fields: dict | None = None) -> dict:
    frm = app["status"]
    if to not in TRANSITIONS.get(frm, set()):
        raise WorkflowError("invalid_transition", f"Cannot move from '{frm}' to '{to}'.")
    fields = dict(fields or {})
    fields.update({"status": to, "updated_at": iso()})
    sets = ", ".join(f"{k}=?" for k in fields)
    # Optimistic concurrency: only succeeds if nobody changed the row since we read it.
    n = db.run(f"UPDATE applications SET {sets}, row_version=row_version+1 WHERE id=? AND row_version=?",
               (*fields.values(), app["id"], app["row_version"]))
    if n != 1:
        raise WorkflowError("conflict", "The application was changed by another action. Reload and try again.")
    audit(db, actor=actor, entity="application", entity_id=app["id"], action="transition", from_status=frm,
          to_status=to, detail={"reason": reason, **(extra or {})})
    return _get_app(db, app["id"], app["user_id"])


# ---------------------------------------------------------------- preparation

def infer_role_family(job: dict, must_have: list[str]) -> str:
    title = (job.get("title") or "").lower()
    if re.search(r"\b(ai|ml|llm|machine learning|genai)\b", title) or "LLMs" in must_have[:3]:
        return "ai_application"
    if re.search(r"\.net|c#|csharp", title) or {"C#", ".NET", "ASP.NET Core"} & set(must_have):
        return "dotnet_backend"
    if "python" in title or {"Python", "FastAPI", "Django", "Flask"} & set(must_have):
        return "python_backend"
    if re.search(r"integration|application engineer|solutions engineer", title):
        return "integration"
    if re.search(r"full ?stack", title):
        return "fullstack"
    return "dotnet_backend"


def screening_answers(candidate: dict, job: dict) -> tuple[list[dict], list[dict]]:
    """Standard screening answers built ONLY from user-confirmed facts. Unknown facts become missing items."""
    country = job.get("country")
    cname = COUNTRY_NAMES.get(country, country or "this country")
    wa = candidate.get("work_authorization") or {}
    answers, missing = [], []

    def add(q, a, source):
        answers.append({"question": q, "answer": a, "source": source})

    add("Full name", candidate.get("full_name"), "settings")
    add("Email", candidate.get("email"), "settings")
    add("Phone", candidate.get("phone"), "settings")
    add("LinkedIn profile", candidate.get("linkedin_url"), "settings")
    add("Current location", candidate.get("current_location"), "settings")
    if country:
        authorised = country in wa
        add(f"Are you legally authorised to work in {cname}?", "Yes" if authorised else "No", "settings")
        if not authorised:
            add("Will you now or in the future require visa sponsorship?",
                "Yes" if candidate.get("requires_sponsorship_outside") else "No", "settings")
    else:
        missing.append({"item": "country", "why": "Job country unknown; work-authorisation answers cannot be "
                        "prepared. Check the posting.", "blocking": True})
    if job.get("remote_type") != "remote" or country:
        add("Are you willing to relocate?", "Yes" if candidate.get("willing_to_relocate") else "No", "settings")
    if candidate.get("notice_period"):
        add("Notice period / earliest start date", candidate["notice_period"], "settings")
    else:
        missing.append({"item": "notice_period", "why": "Notice period not set in Settings (commonly asked).",
                        "blocking": False})
    if candidate.get("salary_expectation"):
        add("Salary expectation", candidate["salary_expectation"], "settings")
    else:
        missing.append({"item": "salary_expectation", "why": "Salary expectation not set; answer manually if asked.",
                        "blocking": False})
    eng = next((l for l in candidate.get("languages", []) if l["language"].lower() == "english"), None)
    if eng and eng["level"] != "unverified":
        add("English proficiency", eng["level"], "settings")
    else:
        missing.append({"item": "english_level", "why": "English level not confirmed in Settings; never guessed.",
                        "blocking": False})
    for a in answers:
        if not a["answer"]:
            missing.append({"item": a["question"], "why": "Value missing in Settings.", "blocking":
                            a["question"] in ("Email", "Full name")})
    answers = [a for a in answers if a["answer"]]
    return answers, missing


def get_or_create(db: Database, job_id: int, actor: str, *, user_id: int) -> dict:
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise WorkflowError("not_found", "Job not found.")
    existing = db.one("SELECT * FROM applications WHERE job_id=? AND user_id=?", (job_id, user_id))
    if existing:
        return existing
    equiv = equivalent_submitted(db, job, user_id=user_id)
    if equiv:
        raise WorkflowError("already_applied", f"You already applied to an equivalent vacancy at this employer "
                                               f"(application #{equiv['id']}, {equiv['status']}).")
    now = iso()
    try:
        with db.transaction():
            app_id = db.insert("INSERT INTO applications(job_id, user_id, status, created_at, updated_at, is_sample) "
                               "VALUES (?, ?, 'shortlisted', ?, ?, ?)", (job_id, user_id, now, now, job["is_sample"]))
            audit(db, actor=actor, entity="application", entity_id=app_id, action="create", to_status="shortlisted",
                  detail={"job_id": job_id})
    except IntegrityError:  # concurrent creator won
        return db.one("SELECT * FROM applications WHERE job_id=? AND user_id=?", (job_id, user_id))
    return _get_app(db, app_id, user_id)


def equivalent_submitted(db: Database, job: dict, *, user_id: int) -> dict | None:
    """Another application OF THIS USER (to a different job row) for the same employer + title + country that
    already reached submission. Guards against reposts that slipped past identity matching."""
    return db.one(
        "SELECT a.* FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.user_id=? AND j.id != ? AND j.employer_id=? "
        "AND j.normalized_title=? AND COALESCE(j.country,'')=COALESCE(?, '') AND (j.requisition_id IS NULL OR "
        "CAST(? AS TEXT) IS NULL OR j.requisition_id = ?) AND a.status IN ('application_started','submitted','submission_uncertain',"
        "'interview','assessment','offer','rejected')",
        (user_id, job["id"], job["employer_id"], job["normalized_title"], job["country"], job["requisition_id"],
         job["requisition_id"]))


def _next_letter_version(db: Database, user_id: int, job_id: int) -> int:
    return (db.scalar("SELECT COALESCE(MAX(version),0) FROM cover_letters WHERE job_id=? AND user_id=?",
                      (job_id, user_id)) or 0) + 1


def prepare(db: Database, job_id: int, actor: str, resume_version_id: int | None, cover_letter: str,
            cover_method: str, *, user_id: int) -> dict:
    """Attach resume variant, cover letter and screening answers; move to awaiting_approval."""
    if resume_version_id is not None and not db.one("SELECT id FROM resume_versions WHERE id=? AND user_id=?",
                                                    (resume_version_id, user_id)):
        raise WorkflowError("not_found", "Resume version not found.")
    app = get_or_create(db, job_id, actor, user_id=user_id)
    if app["status"] not in ("shortlisted", "awaiting_approval", "submission_failed", "closed"):
        raise WorkflowError("not_preparable", f"Application is '{app['status']}'; it cannot be re-prepared.")
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if job["status"] != "open":
        raise WorkflowError("job_not_open", f"Job is {job['status']}.")
    s = settings_mod.get_all(db, user_id)
    answers, missing = screening_answers(s["candidate"], job)
    with db.transaction():
        ver = _next_letter_version(db, user_id, job_id)
        cl_id = db.insert("INSERT INTO cover_letters(job_id, user_id, resume_version_id, version, body, method, status, "
                          "created_at) VALUES (?,?,?,?,?,?,'draft',?)",
                          (job_id, user_id, resume_version_id, ver, cover_letter, cover_method, iso()))
        db.run("UPDATE approvals SET revoked_at=? WHERE application_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
               (iso(), app["id"]))
        app = _get_app(db, app["id"], user_id)
        fields = {"resume_version_id": resume_version_id, "cover_letter_id": cl_id, "answers_json": dumps(answers),
                  "missing_info_json": dumps(missing)}
        if app["status"] == "closed":
            app = _transition(db, app, "shortlisted", actor, "job reopened")
        if app["status"] == "awaiting_approval":
            sets = ", ".join(f"{k}=?" for k in fields)
            db.run(f"UPDATE applications SET {sets}, updated_at=?, row_version=row_version+1 WHERE id=?",
                   (*fields.values(), iso(), app["id"]))
            audit(db, actor=actor, entity="application", entity_id=app["id"], action="re_prepare",
                  detail={"cover_letter_id": cl_id, "resume_version_id": resume_version_id})
            app = _get_app(db, app["id"], user_id)
        else:
            app = _transition(db, app, "awaiting_approval", actor, "application package prepared",
                              {"cover_letter_id": cl_id, "resume_version_id": resume_version_id}, fields)
    return app


def update_answers(db: Database, app_id: int, answers: list[dict], actor: str, *, user_id: int) -> dict:
    app = _get_app(db, app_id, user_id)
    if app["status"] not in ("awaiting_approval", "shortlisted"):
        raise WorkflowError("locked", "Answers can only be edited before approval.")
    clean = []
    for a in answers[:40]:
        q, ans = str(a.get("question", "")).strip()[:300], str(a.get("answer", "")).strip()[:2000]
        if q and ans:
            clean.append({"question": q, "answer": ans, "source": "user"})
    with db.transaction():
        db.run("UPDATE applications SET answers_json=?, updated_at=?, row_version=row_version+1 WHERE id=? AND user_id=?",
               (dumps(clean), iso(), app_id, user_id))
        audit(db, actor=actor, entity="application", entity_id=app_id, action="edit_answers", detail={"n": len(clean)})
    return _get_app(db, app_id, user_id)


def update_cover_letter(db: Database, app_id: int, body: str, actor: str, *, user_id: int) -> dict:
    app = _get_app(db, app_id, user_id)
    if app["status"] not in ("awaiting_approval", "shortlisted"):
        raise WorkflowError("locked", "The cover letter can only be edited before approval.")
    body = body.strip()[:8000]
    with db.transaction():
        ver = _next_letter_version(db, user_id, app["job_id"])
        cl_id = db.insert("INSERT INTO cover_letters(job_id, user_id, resume_version_id, version, body, method, status, "
                          "created_at) VALUES (?,?,?,?,?, 'user', 'draft', ?)",
                          (app["job_id"], user_id, app["resume_version_id"], ver, body, iso()))
        db.run("UPDATE applications SET cover_letter_id=?, updated_at=?, row_version=row_version+1 WHERE id=? "
               "AND user_id=?", (cl_id, iso(), app_id, user_id))
        audit(db, actor=actor, entity="application", entity_id=app_id, action="edit_cover_letter",
              detail={"cover_letter_id": cl_id})
    return _get_app(db, app_id, user_id)


# ---------------------------------------------------------------- approval

def snapshot(db: Database, app: dict) -> dict:
    job = db.one("SELECT j.*, e.name AS employer_name FROM jobs j JOIN employers e ON e.id=j.employer_id "
                 "WHERE j.id=?", (app["job_id"],))
    uid = app["user_id"]
    resume = db.one("SELECT id, structured_json FROM resume_versions WHERE id=? AND user_id=?",
                    (app["resume_version_id"], uid)) if app["resume_version_id"] else None
    cl = db.one("SELECT id, body FROM cover_letters WHERE id=? AND user_id=?", (app["cover_letter_id"], uid)) \
        if app["cover_letter_id"] else None
    return {
        "application_id": app["id"],
        "job_id": job["id"],
        "job_key": job["job_key"],
        "employer": job["employer_name"],
        "title": job["title"],
        "requisition_id": job["requisition_id"],
        "destination_url": canonical_url(job["apply_url"]),
        "resume_version_id": resume["id"] if resume else None,
        "resume_sha256": hashlib.sha256((resume["structured_json"] if resume else "").encode()).hexdigest(),
        "cover_letter_id": cl["id"] if cl else None,
        "cover_letter_sha256": hashlib.sha256((cl["body"] if cl else "").encode()).hexdigest(),
        "answers": loads(app["answers_json"], []),
    }


def fingerprint(snap: dict) -> str:
    return hashlib.sha256(dumps(snap).encode("utf-8")).hexdigest()


def verify_destination(url: str | None, job: dict, db: Database) -> str:
    """Destination must be the https apply URL delivered by the official source API for this job."""
    if not url:
        raise WorkflowError("no_destination", "This job has no application URL.")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise WorkflowError("insecure_destination", "Application URL is not HTTPS.")
    if not host or re.match(r"^[\d.]+$", host) or host in ("localhost",) or host.endswith(".local"):
        raise WorkflowError("unverified_destination", "Application URL host is not a public website.")
    listing_hosts = {(urlsplit(l["url"] or "").hostname or "").lower()
                     for l in db.all("SELECT url FROM job_listings WHERE job_id=?", (job["id"],))}
    trusted = any(host == h or host.endswith("." + h) for h in TRUSTED_ATS_HOSTS) or host in listing_hosts
    if not trusted:
        raise WorkflowError("unverified_destination",
                            f"Application host '{host}' is not a known ATS and does not match the listing; "
                            "open the listing manually and verify it.")
    return url


def approval_gate_checks(db: Database, app: dict, s: dict) -> list[str]:
    """Reasons approval is currently impossible (empty list = can approve)."""
    problems = []
    job = db.one("SELECT * FROM jobs WHERE id=?", (app["job_id"],))
    if app["status"] != "awaiting_approval":
        problems.append(f"Status is '{app['status']}', expected 'awaiting_approval'.")
    if not app["resume_version_id"]:
        problems.append("No resume selected.")
    if job["status"] != "open":
        problems.append(f"Job is {job['status']}.")
    v = parse_iso(job["last_verified_at"])
    if v is None or utcnow() - v > timedelta(hours=s["verification_max_age_hours"]):
        problems.append("Listing was not verified recently; refresh it first.")
    blocking = [m for m in loads(app["missing_info_json"], []) if m.get("blocking")]
    if blocking:
        problems.append("Missing required information: " + ", ".join(m["item"] for m in blocking))
    try:
        verify_destination(job["apply_url"], job, db)
    except WorkflowError as e:
        problems.append(e.message)
    if equivalent_submitted(db, job, user_id=app["user_id"]):
        problems.append("You already applied to an equivalent vacancy at this employer.")
    day, week = usage_counts(db, user_id=app["user_id"])
    if day >= s["max_applications_per_day"]:
        problems.append(f"Daily application limit reached ({s['max_applications_per_day']}).")
    if week >= s["max_applications_per_week"]:
        problems.append(f"Weekly application limit reached ({s['max_applications_per_week']}).")
    return problems


def usage_counts(db: Database, *, user_id: int) -> tuple[int, int]:
    now = utcnow()
    q = "SELECT COUNT(*) FROM applications WHERE started_at >= ? AND external=0 AND user_id=?"
    day = db.scalar(q, (iso(now - timedelta(days=1)), user_id))
    week = db.scalar(q, (iso(now - timedelta(days=7)), user_id))
    return int(day or 0), int(week or 0)


def approve(db: Database, app_id: int, actor: str, reviewed_fingerprint: str, *, user_id: int) -> dict:
    """User approval for ONE application, bound to exactly what they reviewed."""
    if not actor.startswith("user:"):
        raise WorkflowError("not_user", "Only a signed-in user can approve an application.")
    s = settings_mod.get_all(db, user_id)
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        problems = approval_gate_checks(db, app, s)
        if problems:
            raise WorkflowError("gate", " ".join(problems))
        snap = snapshot(db, app)
        fp = fingerprint(snap)
        if fp != reviewed_fingerprint:
            raise WorkflowError("stale_review", "The application changed after you reviewed it (employer, role, URL, "
                                                "resume, cover letter or answers). Review it again.")
        now = utcnow()
        try:
            db.insert("INSERT INTO approvals(application_id, fingerprint, snapshot_json, approved_at, expires_at) "
                      "VALUES (?,?,?,?,?)", (app_id, fp, dumps(snap), iso(now),
                                              iso(now + timedelta(hours=s["approval_ttl_hours"]))))
        except IntegrityError:
            raise WorkflowError("already_approved", "This application already has an active approval.") from None
        app = _transition(db, app, "ready_to_apply", actor, "approved by user", {"fingerprint": fp[:16]})
    return app


AUTO_ACTOR = "agent:auto-apply"


def auto_apply_ineligibility(db: Database, app: dict, s: dict) -> list[str]:
    """Why the auto-apply policy may NOT approve this application (empty list = eligible).
    Deterministic rules only; AI output is never consulted here."""
    from .normalize import url_host
    pol = s["auto_apply"]
    reasons = []
    if not pol.get("enabled") or not pol.get("acknowledged_at"):
        return ["Auto-apply is not enabled."]
    job = db.one("SELECT * FROM jobs WHERE id=?", (app["job_id"],))
    sc = db.one("SELECT score, recommendation, details_json FROM job_scores WHERE job_id=? AND user_id=? "
                "ORDER BY id DESC LIMIT 1", (app["job_id"], app["user_id"]))
    if not sc:
        return ["Job has not been scored."]
    details = loads(sc["details_json"], {})
    if sc["recommendation"] != "apply":
        reasons.append(f"Recommendation is '{sc['recommendation']}', not 'apply'.")
    if sc["score"] < pol["min_score"]:
        reasons.append(f"Score {sc['score']} is below the auto-apply minimum {pol['min_score']}.")
    if details.get("disqualifiers"):
        reasons.append("Job has disqualifiers.")
    sp = details.get("sponsorship") or {}
    cls = "not_needed" if sp.get("needs_sponsorship") is False else sp.get("classification", "unknown")
    if cls not in pol["sponsorship"]:
        reasons.append(f"Sponsorship '{cls}' is not allowed by the auto-apply policy.")
    allowed_countries = pol["countries"] or settings_mod.all_target_countries(s) + (["IN"] if s["include_india_remote"] else [])
    if job["country"] not in allowed_countries:
        reasons.append(f"Country '{job['country']}' is not in the auto-apply countries.")
    host = url_host(job["apply_url"]) or ""
    ats = next((a for a, h in (("greenhouse", "greenhouse.io"), ("lever", "lever.co"), ("ashby", "ashbyhq.com"))
                if host == h or host.endswith("." + h)), None)
    if ats not in pol["ats"]:
        reasons.append(f"Application site '{host}' is not a supported auto-apply ATS.")
    if any(m.get("blocking") for m in loads(app["missing_info_json"], [])):
        reasons.append("Required information is missing.")
    since = iso(utcnow() - timedelta(days=1))
    today = int(db.scalar("SELECT COUNT(*) FROM applications WHERE auto_apply=1 AND started_at >= ? AND user_id=?",
                          (since, app["user_id"])) or 0)
    if today >= pol["max_per_day"]:
        reasons.append(f"Auto-apply daily limit reached ({pol['max_per_day']}).")
    return reasons


def policy_approve(db: Database, app_id: int, *, user_id: int) -> dict:
    """Approval issued by the user's enabled auto-apply policy (instead of a click). Same fingerprinting,
    expiry and single-use semantics as a user approval, plus every normal approval gate."""
    s = settings_mod.get_all(db, user_id)
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        problems = approval_gate_checks(db, app, s) + auto_apply_ineligibility(db, app, s)
        if problems:
            raise WorkflowError("not_eligible", " ".join(problems))
        snap = snapshot(db, app)
        snap["approved_by"] = "policy"
        snap["policy"] = {k: s["auto_apply"][k] for k in ("mode", "min_score", "sponsorship", "acknowledged_at")}
        fp = fingerprint({k: v for k, v in snap.items() if k not in ("approved_by", "policy")})
        now = utcnow()
        try:
            db.insert("INSERT INTO approvals(application_id, fingerprint, snapshot_json, approved_at, expires_at, "
                      "approved_by) VALUES (?,?,?,?,?, 'policy')",
                      (app_id, fp, dumps(snap), iso(now), iso(now + timedelta(hours=min(24, s["approval_ttl_hours"])))))
        except IntegrityError:
            raise WorkflowError("already_approved", "This application already has an active approval.") from None
        db.run("UPDATE applications SET auto_apply=1 WHERE id=? AND user_id=?", (app_id, user_id))
        app = _transition(db, _get_app(db, app_id, user_id), "ready_to_apply", "policy:auto-apply",
                          "approved by your auto-apply policy", {"fingerprint": fp[:16]})
    return app


def revoke_approval(db: Database, app_id: int, actor: str, reason: str = "revoked by user", *,
                    user_id: int) -> dict:
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        db.run("UPDATE approvals SET revoked_at=? WHERE application_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
               (iso(), app_id))
        if app["status"] == "ready_to_apply":
            app = _transition(db, app, "awaiting_approval", actor, reason)
    return app


def active_approval(db: Database, app_id: int, *, user_id: int | None) -> dict | None:
    if user_id is None:
        return db.one("SELECT * FROM approvals WHERE application_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
                      (app_id,))
    return db.one("SELECT ap.* FROM approvals ap JOIN applications a ON a.id=ap.application_id WHERE "
                  "ap.application_id=? AND a.user_id=? AND ap.consumed_at IS NULL AND ap.revoked_at IS NULL",
                  (app_id, user_id))


def start_application(db: Database, app_id: int, actor: str, *, user_id: int) -> dict:
    """Consume the approval and hand over to the assisted workflow (user submits on the employer's site).
    Returns {'application', 'destination_url'}."""
    if not (actor.startswith("user:") or actor == AUTO_ACTOR):
        raise WorkflowError("not_user", "Only a signed-in user (or the enabled auto-apply policy) can start an "
                                        "approved application.")
    refusal: WorkflowError | None = None
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        if app["status"] != "ready_to_apply":
            raise WorkflowError("not_ready", f"Application is '{app['status']}', not ready to apply.")
        ap = active_approval(db, app_id, user_id=user_id)
        if not ap:
            raise WorkflowError("no_approval", "No active approval for this application.")
        if actor == AUTO_ACTOR:
            policy = settings_mod.get(db, "auto_apply", user_id)
            if ap.get("approved_by") != "policy":
                raise WorkflowError("not_policy", "Auto-apply may only use approvals created by the auto-apply policy.")
            if not (policy.get("enabled") and policy.get("acknowledged_at")):
                raise WorkflowError("policy_disabled", "Auto-apply is disabled; nothing will be submitted automatically.")
        # Revocations below must be COMMITTED before refusing; raising inside the transaction would roll them
        # back and leave a stale approval usable.
        if (parse_iso(ap["expires_at"]) or utcnow()) <= utcnow():
            db.run("UPDATE approvals SET revoked_at=? WHERE id=?", (iso(), ap["id"]))
            _transition(db, app, "awaiting_approval", "system", "approval expired")
            refusal = WorkflowError("approval_expired", "Your approval expired. Review and approve again.")
        elif fingerprint(snapshot(db, app)) != ap["fingerprint"]:
            db.run("UPDATE approvals SET revoked_at=? WHERE id=?", (iso(), ap["id"]))
            _transition(db, app, "awaiting_approval", "system", "approved content changed")
            refusal = WorkflowError("approval_mismatch", "Something changed since approval (e.g. application URL "
                                                         "or resume). The approval was revoked; review again.")
        else:
            job = db.one("SELECT * FROM jobs WHERE id=?", (app["job_id"],))
            if job["status"] != "open":
                raise WorkflowError("job_not_open", f"Job is {job['status']}.")
            url = verify_destination(job["apply_url"], job, db)
            if equivalent_submitted(db, job, user_id=user_id):
                raise WorkflowError("already_applied", "An equivalent vacancy already has a submitted application.")
            n = db.run("UPDATE approvals SET consumed_at=? WHERE id=? AND consumed_at IS NULL AND revoked_at IS NULL",
                       (iso(), ap["id"]))
            if n != 1:
                raise WorkflowError("approval_used", "This approval was already used.")
            app = _transition(db, app, "application_started", actor, "assisted application opened",
                              {"destination": url}, {"started_at": iso(), "submitted_url": url})
    if refusal:
        raise refusal
    return {"application": app, "destination_url": url}


SUBMIT_EVIDENCE_MIN = 8


def record_outcome(db: Database, app_id: int, outcome: str, actor: str, *, user_id: int, reference_number: str = "",
                   confirmation_text: str = "", file_id: int | None = None, failure_stage: str = "",
                   reason: str = "") -> dict:
    """Record what happened on the employer's site. 'submitted' requires evidence."""
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        if file_id is not None and not db.one("SELECT id FROM files WHERE id=? AND user_id=?", (file_id, user_id)):
            raise WorkflowError("not_found", "Evidence file not found.")
        if app["status"] not in ("application_started", "submission_uncertain"):
            raise WorkflowError("invalid_transition", f"Cannot record an outcome for status '{app['status']}'.")
        if outcome == "submitted":
            ref = reference_number.strip()[:200]
            conf = confirmation_text.strip()[:4000]
            if not ref and len(conf) < SUBMIT_EVIDENCE_MIN and not file_id:
                raise WorkflowError("no_evidence", "Provide a reference number, the confirmation message/email text, "
                                                   "or a screenshot of the confirmation page.")
            evidence = {"reference_number": ref or None, "confirmation_text": conf or None, "file_id": file_id,
                        "recorded_by": actor, "recorded_at": iso()}
            return _transition(db, app, "submitted", actor, "submission confirmed with evidence",
                               {"evidence_types": [k for k, v in evidence.items() if v and k in
                                                   ("reference_number", "confirmation_text", "file_id")]},
                               {"submitted_at": iso(), "reference_number": ref or None,
                                "submission_evidence_json": dumps(evidence)})
        if outcome == "failed":
            if failure_stage not in ("before_submit", "after_submit_unknown"):
                raise WorkflowError("bad_stage", "Say whether it failed before you pressed submit.")
            if failure_stage == "after_submit_unknown":
                if app["status"] == "submission_uncertain":
                    raise WorkflowError("still_uncertain", "Already uncertain; check your email or the portal "
                                                           "and record the final outcome.")
                return _transition(db, app, "submission_uncertain", actor, reason or "outcome unknown after submit")
            return _transition(db, app, "submission_failed", actor, reason or "failed before submission",
                               {"stage": failure_stage})
        if outcome == "uncertain":
            if app["status"] == "submission_uncertain":
                return app
            return _transition(db, app, "submission_uncertain", actor, reason or "user unsure")
        raise WorkflowError("bad_outcome", "Unknown outcome.")


def retry_after_failure(db: Database, app_id: int, actor: str, *, user_id: int) -> dict:
    """Only failures BEFORE submission can be retried, and a fresh approval is always required."""
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        if app["status"] != "submission_failed":
            raise WorkflowError("not_failed", "Only failed applications can be retried.")
        last = db.one("SELECT detail_json FROM audit_events WHERE entity='application' AND entity_id=? "
                      "AND to_status='submission_failed' ORDER BY id DESC LIMIT 1", (str(app_id),))
        if loads(last["detail_json"] if last else None, {}).get("stage") != "before_submit":
            raise WorkflowError("not_recoverable", "This failure may have happened after submission; verify first.")
        return _transition(db, app, "awaiting_approval", actor, "retry requested; new approval required")


def mark_applied_externally(db: Database, job_id: int, actor: str, applied_on: str, note: str, *,
                            user_id: int) -> dict:
    """User says they already applied outside JobAgent. Recorded as their declaration (evidence type)."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", applied_on or ""):
        raise WorkflowError("bad_date", "Date must be YYYY-MM-DD.")
    app = get_or_create(db, job_id, actor, user_id=user_id)
    with db.transaction():
        app = _get_app(db, app["id"], user_id)
        if app["status"] not in PRE_SUBMIT | {"submission_failed", "closed"}:
            raise WorkflowError("already_recorded", f"Application already '{app['status']}'.")
        db.run("UPDATE approvals SET revoked_at=? WHERE application_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
               (iso(), app["id"]))
        evidence = {"type": "user_declared_external", "applied_on": applied_on, "note": note.strip()[:1000],
                    "recorded_by": actor, "recorded_at": iso()}
        n = db.run("UPDATE applications SET status='submitted', external=1, submitted_at=?, submission_evidence_json=?, "
                   "updated_at=?, row_version=row_version+1 WHERE id=? AND row_version=? AND user_id=?",
                   (applied_on + "T00:00:00+00:00", dumps(evidence), iso(), app["id"], app["row_version"], user_id))
        if n != 1:
            raise WorkflowError("conflict", "Application changed concurrently; retry.")
        audit(db, actor=actor, entity="application", entity_id=app["id"], action="mark_external",
              from_status=app["status"], to_status="submitted", detail={"applied_on": applied_on})
    return _get_app(db, app["id"], user_id)


def update_progress(db: Database, app_id: int, actor: str, to_status: str | None = None, *, user_id: int,
                    notes: str | None = None, interview_at: str | None = None, follow_up_at: str | None = None,
                    rejection_reason: str | None = None, offer_details: str | None = None,
                    expected_version: int | None = None) -> dict:
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        if expected_version is not None and app["row_version"] != expected_version:
            raise WorkflowError("conflict", "Application was updated elsewhere. Reload and try again.")
        fields = {}
        for k, v, n in (("notes", notes, 5000), ("rejection_reason", rejection_reason, 1000),
                        ("offer_details", offer_details, 2000)):
            if v is not None:
                fields[k] = v.strip()[:n]
        for k, v in (("interview_at", interview_at), ("follow_up_at", follow_up_at)):
            if v is not None:
                if v and not re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$", v):
                    raise WorkflowError("bad_date", f"{k} must be YYYY-MM-DD or YYYY-MM-DDTHH:MM.")
                fields[k] = v or None
        if to_status and to_status != app["status"]:
            if to_status == "submitted":
                raise WorkflowError("no_evidence", "Use 'record submission' with evidence to mark as submitted.")
            return _transition(db, app, to_status, actor, "status update", fields=fields)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            n = db.run(f"UPDATE applications SET {sets}, updated_at=?, row_version=row_version+1 WHERE id=? "
                       "AND row_version=? AND user_id=?", (*fields.values(), iso(), app_id, app["row_version"], user_id))
            if n != 1:
                raise WorkflowError("conflict", "Application was updated elsewhere. Reload and try again.")
            audit(db, actor=actor, entity="application", entity_id=app_id, action="update",
                  detail={"fields": sorted(fields)})
        return _get_app(db, app_id, user_id)


def correct_status(db: Database, app_id: int, actor: str, to_status: str, reason: str, *, user_id: int) -> dict:
    """Manual correction outside normal transitions (audited). Cannot fabricate a submission."""
    if to_status not in STATUS_LABELS or to_status == "discovered":
        raise WorkflowError("bad_status", "Unknown status.")
    if to_status in ("submitted", "application_started", "ready_to_apply"):
        raise WorkflowError("not_allowed", "Use the approval / submission flow (or 'applied outside JobAgent') for "
                                           "this status.")
    if len(reason.strip()) < 5:
        raise WorkflowError("reason_required", "Give a reason for the correction.")
    with db.transaction():
        app = _get_app(db, app_id, user_id)
        n = db.run("UPDATE applications SET status=?, updated_at=?, row_version=row_version+1 WHERE id=? "
                   "AND row_version=? AND user_id=?", (to_status, iso(), app_id, app["row_version"], user_id))
        if n != 1:
            raise WorkflowError("conflict", "Concurrent change; retry.")
        db.run("UPDATE approvals SET revoked_at=? WHERE application_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
               (iso(), app_id))
        audit(db, actor=actor, entity="application", entity_id=app_id, action="manual_correction",
              from_status=app["status"], to_status=to_status, detail={"reason": reason.strip()[:500]})
    return _get_app(db, app_id, user_id)


def sweep_stale_starts(db: Database, hours: int, *, user_id: int | None) -> list[int]:
    """Started applications with no recorded outcome become 'submission_uncertain' (never auto-resubmitted).
    user_id=None sweeps every user (system job)."""
    cutoff = iso(utcnow() - timedelta(hours=hours))
    ids = []
    q, p = "SELECT * FROM applications WHERE status='application_started' AND started_at < ?", [cutoff]
    if user_id is not None:
        q, p = q + " AND user_id=?", p + [user_id]
    for a in db.all(q, p):
        try:
            with db.transaction():
                _transition(db, a, "submission_uncertain", "system", f"no outcome recorded within {hours}h")
            ids.append(a["id"])
        except WorkflowError:
            continue
    return ids


def close_for_job(db: Database, job_id: int) -> None:
    """Job closed upstream (shared job): every user's pre-submission application moves to 'closed';
    submitted history is untouched."""
    for a in db.all("SELECT * FROM applications WHERE job_id=?", (job_id,)):
        if a["status"] in PRE_SUBMIT | {"submission_failed"}:
            with db.transaction():
                db.run("UPDATE approvals SET revoked_at=? WHERE application_id=? AND consumed_at IS NULL "
                       "AND revoked_at IS NULL", (iso(), a["id"]))
                _transition(db, a, "closed", "system", "job closed at source")
