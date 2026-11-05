"""Agent runs: discover -> normalise -> deduplicate -> verify -> assess sponsorship -> score -> prepare drafts.
Never submits anything: preparation stops at 'awaiting_approval'.

Multi-user: discovery is installation-wide (one fetch for everyone, using the global source settings and the union of
all approved users' target countries). Scoring, preparation and policy approvals then run PER APPROVED USER with that
user's own resume and settings, round-robin within the time budget (a run that runs out of time resumes with the next
user on the following run)."""
from __future__ import annotations

import hashlib
import time
import uuid
from datetime import timedelta

from . import applications as apps
from . import jd, letters, prep
from . import settings as settings_mod
from .ai import AIError, AIService, Provider, analyze_job_ai, cover_letter_ai, letter_violations
from .config import AppConfig, validate_config
from .db import Database, IntegrityError, LockHeld, audit, dumps, iso, loads, named_lock, utcnow
from .http import FetchError, HttpClient
from .ingest import mark_closures, upsert_raw
from .matching import candidate_profile, score_job
from .resume import store as resume_store
from .resume.parser import ResumeError
from .sources import build_sources
from .sponsorship import classify, load_register, refresh_uk_register, register_lookup

CURSOR_KEY = "_discovery_cursor"
USER_CURSOR_KEY = "_scoring_user_cursor"


class RunContext:
    def __init__(self, db: Database, kind: str, trigger: str, cfg: AppConfig, settings: dict,
                 user_id: int | None = None):
        self.db, self.kind, self.trigger = db, kind, trigger
        self.id = str(uuid.uuid4())
        self.user_id = user_id
        self.stats = {"sources_queried": 0, "listings_seen": 0, "new_jobs": 0, "duplicates": 0, "updated": 0,
                      "reopened": 0, "out_of_scope": 0, "invalid": 0, "closed": 0, "scored": 0, "shortlisted": 0,
                      "prepared": 0, "submitted_confirmed": 0, "uncertain_marked": 0, "failures": 0, "policy_approved": 0,
                      "users_processed": 0}
        self.sources: list[dict] = []
        self.errors: list[str] = []
        self.rate_limits: list[str] = []
        with db.transaction():
            db.run("INSERT INTO runs(id, kind, trigger, status, started_at, config_json, user_id) "
                   "VALUES (?,?,?,'running',?,?,?)",
                   (self.id, kind, trigger, iso(), dumps({"app": cfg.redacted(), "settings": _settings_summary(settings)}),
                    user_id))

    def decision(self, job_id: int | None, decision: str, reason: str, user_id: int | None = None):
        """Record why a job was handled. user_id=None: shared discovery decision; otherwise private to that user."""
        if job_id is None:
            return
        with self.db.transaction():
            exists = self.db.one("SELECT 1 AS x FROM run_decisions WHERE run_id=? AND job_id=? AND decision=? AND "
                                 "COALESCE(user_id, 0)=?", (self.id, job_id, decision, user_id or 0))
            if not exists:
                try:
                    self.db.run("INSERT INTO run_decisions(run_id, job_id, decision, reason, at, user_id) "
                                "VALUES (?,?,?,?,?,?)", (self.id, job_id, decision, reason[:500], iso(), user_id))
                except IntegrityError:
                    pass

    def finish(self, status: str, ai: AIService | None = None):
        usage = self.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) AS tin, COALESCE(SUM(tokens_out),0) AS tout, "
                            "COALESCE(SUM(cost_usd),0) AS cost FROM ai_usage WHERE run_id=? AND cached=0", (self.id,))
        stats = dict(self.stats, rate_limits=self.rate_limits)
        with self.db.transaction():
            self.db.run("UPDATE runs SET status=?, finished_at=?, sources_json=?, stats_json=?, errors_json=?, ai_calls=?, "
                        "ai_tokens_in=?, ai_tokens_out=?, ai_cost_usd=? WHERE id=?",
                        (status, iso(), dumps(self.sources), dumps(stats), dumps(self.errors[:100]), usage["n"],
                         usage["tin"], usage["tout"], round(float(usage["cost"]), 6), self.id))
        audit(self.db, actor="agent", entity="run", entity_id=self.id, action="finish", detail={"status": status,
              **{k: v for k, v in self.stats.items() if v}})


def _settings_summary(s: dict) -> dict:
    """Installation-wide settings only (runs are shared records; per-user preferences stay private)."""
    return {"operating_mode": s.get("operating_mode"),
            "sources": {k: (v.get("boards") or v.get("sites") or v.get("enabled")) for k, v in s["sources"].items()},
            "ai": {k: s["ai"][k] for k in ("enabled", "model", "fallback_model", "run_budget_usd")}}


def profile_version(resume: dict | None, s: dict) -> str:
    basis = dumps({"resume": resume["id"] if resume else None, "analysis": (resume or {}).get("analysis_json"),
                   "candidate": s["candidate"], "weights": s["weights"], "targets": s["target_countries"],
                   "roles": s["preferred_roles"], "excl": s["excluded_title_terms"], "min": s["min_match_score"],
                   "policy": s["sponsorship_policy"], "arr": s["work_arrangements"], "sal": s["min_salary"],
                   "india": s["include_india_remote"]})
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def ensure_resume(db: Database, cfg: AppConfig, *, user_id: int) -> dict | None:
    """The user's canonical resume. JOBAGENT_RESUME_PATH (a local file) is only ever imported for the admin."""
    r = resume_store.canonical(db, user_id=user_id)
    if r or not cfg.resume_path:
        return r
    if db.scalar("SELECT role FROM users WHERE id=?", (user_id,)) != "admin":
        return None
    return resume_store.import_original(db, cfg.resume_path, actor="agent", user_id=user_id)


# ---------------------------------------------------------------- scoring

def score_and_store(db: Database, job_id: int, profile: dict, s: dict, pversion: str, ai: AIService | None = None,
                    run_id: str | None = None, use_ai: bool = False, *, user_id: int) -> dict:
    """Score one (shared) job for ONE user. Sponsorship evidence is job-level and shared; whether this user needs
    sponsorship (needs_sponsorship) is computed from the user's own candidate facts and stored in their score."""
    job = db.one("SELECT j.*, e.name AS employer_name, e.uk_sponsor_status, e.uk_sponsor_routes, "
                 "e.uk_sponsor_checked_at FROM jobs j JOIN employers e ON e.id=j.employer_id WHERE j.id=?", (job_id,))
    analysis = jd.analyze(job["title"], job["description_text"])
    if job["experience_min_years"] is not None:
        analysis["experience_years"] = job["experience_min_years"]
    sp = classify(job["description_text"], country=job["country"], candidate=s["candidate"],
                  employer={"uk_sponsor_status": job["uk_sponsor_status"], "uk_sponsor_routes": job["uk_sponsor_routes"],
                            "uk_sponsor_checked_at": job["uk_sponsor_checked_at"]},
                  source_url=job["canonical_url"])
    if job["remote_type"] == "remote" and job["remote_scope"] in ("india_allowed", "worldwide"):
        sp["needs_sponsorship"] = False
    method = "rules"
    ai_info = None
    if use_ai and ai and ai.available and getattr(ai, "user_id", user_id) == user_id:
        try:
            ai_info = analyze_job_ai(ai, job["title"], job["description_text"])
            method = "rules+ai"
            for sk in ai_info["must_have"]:
                if sk not in analysis["must_have"] and sk not in analysis["nice_to_have"]:
                    analysis["must_have"].append(sk)
            for sk in ai_info["nice_to_have"]:
                if sk not in analysis["must_have"] and sk not in analysis["nice_to_have"]:
                    analysis["nice_to_have"].append(sk)
            if analysis["experience_years"] is None and ai_info["min_years"] is not None:
                analysis["experience_years"] = ai_info["min_years"]
            if sp["classification"] == "unknown" and ai_info["sponsorship"]:
                stances = {x["stance"] for x in ai_info["sponsorship"]}
                if len(stances) == 1:
                    sp["classification"] = "confirmed" if "offered" in stances else "not_supported"
                    sp["note"] = "Based on a verbatim statement in the posting identified by AI (quote shown)."
                for x in ai_info["sponsorship"]:
                    sp["evidence"].append({"kind": "posting_ai_quote_" + x["stance"], "text": x["quote"],
                                           "source": job["canonical_url"] or "job posting", "checked_at": iso()})
        except AIError as e:
            ai_info = {"error": str(e)}
    job_d = dict(job)
    job_d["employment_type"] = job["employment_type"]
    result = score_job(job_d, analysis, sp, profile, s)
    details = dict(result, analysis={k: analysis[k] for k in ("must_have", "nice_to_have", "experience_years",
                                                               "experience_evidence", "languages", "education",
                                                               "relocation", "salary", "responsibilities")},
                   sponsorship=sp, ai=ai_info)
    details_hash = hashlib.sha256(dumps(details).encode()).hexdigest()
    latest = db.one("SELECT details_json FROM job_scores WHERE job_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                    (job_id, user_id))
    with db.transaction():
        prev_sp = db.one("SELECT classification, evidence_json FROM sponsorship_assessments WHERE job_id=? "
                         "ORDER BY id DESC LIMIT 1", (job_id,))
        if not prev_sp or prev_sp["classification"] != sp["classification"] or \
                loads(prev_sp["evidence_json"], []) != sp["evidence"]:
            db.run("INSERT INTO sponsorship_assessments(job_id, classification, evidence_json, method, assessed_at) "
                   "VALUES (?,?,?,?,?)", (job_id, sp["classification"], dumps(sp["evidence"]), method, iso()))
        if not latest or hashlib.sha256(latest["details_json"].encode()).hexdigest() != details_hash:
            db.run("INSERT INTO job_scores(job_id, user_id, run_id, score, recommendation, breakdown_json, details_json, "
                   "weights_json, profile_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (job_id, user_id, run_id, result["score"], result["recommendation"], dumps(result["components"]),
                    dumps(details), dumps(s["weights"]), pversion, iso()))
        else:
            db.run("UPDATE job_scores SET profile_version=? WHERE id=(SELECT MAX(id) FROM job_scores WHERE job_id=? "
                   "AND user_id=?)", (pversion, job_id, user_id))
        db.run("INSERT INTO prep_plans(user_id, job_id, plan_json, created_at) VALUES (?,?,?,?) "
               "ON CONFLICT(user_id, job_id) DO UPDATE SET plan_json=excluded.plan_json, created_at=excluded.created_at",
               (user_id, job_id, dumps(prep.build_plan(job_d, result, analysis)), iso()))
    return details


def latest_score(db: Database, job_id: int, *, user_id: int) -> dict | None:
    r = db.one("SELECT * FROM job_scores WHERE job_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (job_id, user_id))
    if r:
        r["details"] = loads(r["details_json"], {})
    return r


# ---------------------------------------------------------------- preparation

def prepare_application(db: Database, job_id: int, actor: str, profile: dict, s: dict,
                        ai: AIService | None = None, *, user_id: int) -> dict:
    job = db.one("SELECT j.*, e.name AS employer_name FROM jobs j JOIN employers e ON e.id=j.employer_id WHERE j.id=?",
                 (job_id,))
    if not job:
        raise apps.WorkflowError("not_found", "Job not found.")
    if ai is not None and getattr(ai, "user_id", user_id) != user_id:
        ai = None   # never use another user's AI service
    sc = latest_score(db, job_id, user_id=user_id)
    must = (sc or {}).get("details", {}).get("analysis", {}).get("must_have", [])
    family = apps.infer_role_family(job, must)
    country = job["country"] if job["country"] in settings_mod.COUNTRY_NAMES else None
    if s.get("resume_style", {}).get("tailor_per_job", True):
        # One-page resume tailored to THIS posting, the employer's style profile and the country's conventions,
        # optimised toward the ATS target without adding facts.
        variant = resume_store.create_job_resume(db, job_id, actor, ai=ai, user_id=user_id)
    else:
        variant = resume_store.create_variant(db, family, country, actor=actor, ai=ai, user_id=user_id)
    canonical = resume_store.canonical(db, user_id=user_id)
    structured = canonical["structured"]
    letter, method = letters.template_letter(structured, s["candidate"], profile, dict(job), job["employer_name"],
                                             must), "template"
    if ai and ai.available:
        try:
            facts = letters.facts_for_letter(structured, s["candidate"], profile, dict(job))
            draft = cover_letter_ai(ai, facts, job["title"], job["employer_name"], job["description_text"])
            violations = letter_violations(draft, structured.get("text", "") + " " + job["employer_name"] + " "
                                           + job["title"], set(profile["skills"]))
            if not violations:
                letter, method = draft, "ai"
            else:
                audit(db, actor="agent", entity="job", entity_id=job_id, action="ai_letter_rejected",
                      detail={"violations": violations[:5], "user_id": user_id})
        except AIError:
            pass
    return apps.prepare(db, job_id, actor, variant["id"], letter, method, user_id=user_id)


# ---------------------------------------------------------------- run

def _user_order(db: Database, user_ids: list[int], use_cursor: bool) -> list[int]:
    """Round-robin: start after the user the previous (time-boxed) run finished with."""
    if not use_cursor or len(user_ids) < 2:
        return list(user_ids)
    row = db.one("SELECT value_json FROM settings WHERE key=?", (USER_CURSOR_KEY,))
    last = int(loads(row["value_json"], 0) or 0) if row else 0
    after = [u for u in user_ids if u > last]
    before = [u for u in user_ids if u <= last]
    return after + before


def _save_user_cursor(db: Database, uid: int) -> None:
    with db.transaction():
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "value_json=excluded.value_json, updated_at=excluded.updated_at", (USER_CURSOR_KEY, dumps(uid), iso()))


def run_agent(db: Database, cfg: AppConfig, *, trigger: str = "manual", http: HttpClient | None = None,
              provider: Provider | None = None, time_budget_s: float | None = None, prepare: bool = True,
              discover: bool = True, user_ids: list[int] | None = None, triggered_by: int | None = None) -> dict:
    """One idempotent agent run. Returns the run summary. Safe to call repeatedly or concurrently.
    `user_ids` limits scoring/preparation to those users (default: every approved user)."""
    g = settings_mod.discovery_settings(db)
    try:
        lock_cm = named_lock(db, "agent_run", ttl_minutes=30 if time_budget_s else 120)
        lock_cm.__enter__()
    except LockHeld as e:
        ctx = RunContext(db, "agent", trigger, cfg, g, triggered_by)
        ctx.errors.append(str(e))
        ctx.finish("aborted")
        return {"run_id": ctx.id, "status": "aborted", "error": str(e)}
    ctx = RunContext(db, "agent", trigger, cfg, g, triggered_by)
    started = time.monotonic()
    deadline = started + time_budget_s if time_budget_s else None
    owns_http = http is None
    http = http or HttpClient(user_agent=cfg.user_agent, deadline=deadline)
    if deadline:
        http.deadline = deadline
    provider = provider or Provider()
    status = "succeeded"
    per_user: dict[int, dict] = {}
    try:
        problems = validate_config(cfg)
        if problems:
            ctx.errors += problems
            status = "failed"
            return {"run_id": ctx.id, "status": status, "errors": problems}

        def time_left(reserve: float = 0.0) -> bool:
            return deadline is None or time.monotonic() + reserve < deadline

        if discover:
            status = _discover(db, http, g, ctx, deadline, time_left, status)

        approved = settings_mod.approved_user_ids(db)
        targets = [u for u in approved if user_ids is None or u in user_ids]
        order = _user_order(db, targets, use_cursor=bool(deadline) and user_ids is None)
        for idx, uid in enumerate(order):
            if not time_left(5):
                status = "partial"
                ctx.errors.append(f"Time budget reached; {len(order) - idx} user(s) will be scored next run.")
                break
            ust, finished = _process_user(db, cfg, ctx, uid, provider, prepare, time_left)
            per_user[uid] = ust
            ctx.stats["users_processed"] += 1
            if not finished:
                status = "partial"
                break
            if deadline and user_ids is None:
                _save_user_cursor(db, uid)
        moved = 0
        for uid in targets:
            hours = settings_mod.get(db, "uncertain_after_hours", uid)
            moved += len(apps.sweep_stale_starts(db, hours, user_id=uid))
        ctx.stats["uncertain_marked"] = moved
        ctx.stats["submitted_confirmed"] = int(db.scalar("SELECT COUNT(*) FROM applications WHERE status='submitted' "
                                                         "AND updated_at >= ?", (iso(utcnow() - timedelta(days=1)),)) or 0)
    except Exception as e:  # dependency failure: record and exit safely
        status = "failed"
        ctx.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        if owns_http:
            http.close()
        ctx.finish(status)
        lock_cm.__exit__(None, None, None)
    return {"run_id": ctx.id, "status": status, "stats": ctx.stats, "errors": ctx.errors[:20],
            "per_user": per_user}


def _discover(db: Database, http: HttpClient, s: dict, ctx: RunContext, deadline, time_left, status: str) -> str:
    """Installation-wide discovery (shared jobs). Returns the updated run status."""
    if s["sources"]["uk_sponsor_register"]["enabled"] and time_left(20):
        try:
            info = refresh_uk_register(db, http, s["sources"]["uk_sponsor_register"]["max_age_days"])
            ctx.sources.append({"source": "uk_sponsor_register", **info})
        except FetchError as e:
            ctx.errors.append(f"UK sponsor register: {e}")
            status = "partial"
    from .mnc import build_mnc_sources
    countries = settings_mod.all_target_countries(s) + (["IN"] if s["include_india_remote"] else [])
    # Top-MNC career-site feeds first (the user's priority), then the job-board sources.
    items = build_mnc_sources(http, s["mnc"], countries) + build_sources(http, s["sources"])
    cursor_row = db.one("SELECT value_json FROM settings WHERE key=?", (CURSOR_KEY,))
    start_at = int(loads(cursor_row["value_json"], 0)) if (cursor_row and deadline) else 0
    if start_at >= len(items):
        start_at = 0
    for idx in range(start_at, len(items)):
        if not time_left(8):
            status = "partial"
            ctx.errors.append(f"Time budget reached; will resume at source {idx + 1}/{len(items)} next run.")
            _save_cursor(db, idx)
            break
        src, board = items[idx]
        res = src.fetch(board)
        ctx.stats["sources_queried"] += 1
        ctx.sources.append({"source": src.name, "board": board, "jobs": len(res.jobs), "complete": res.complete,
                            "error": res.error})
        if res.error:
            ctx.errors.append(f"{src.name}/{board or '-'}: {res.error}")
            if "Rate limited" in res.error:
                ctx.rate_limits.append(f"{src.name}/{board}")
            status = "partial"
        seen = set()
        for raw in res.jobs:
            ctx.stats["listings_seen"] += 1
            seen.add(raw.source_job_id)
            try:
                decision, job_id, reason = upsert_raw(db, raw, s)
            except Exception as e:  # one bad listing must not abort the run
                ctx.stats["failures"] += 1
                ctx.errors.append(f"{src.name}:{raw.source_job_id}: {type(e).__name__}: {str(e)[:120]}")
                continue
            key = {"new": "new_jobs", "duplicate": "duplicates", "updated": "updated", "reopened": "reopened",
                   "out_of_scope": "out_of_scope", "invalid": "invalid"}[decision]
            ctx.stats[key] += 1
            if decision != "out_of_scope":
                ctx.decision(job_id, decision, reason)
        if res.complete and not res.error:
            for jid in mark_closures(db, src.name, board, seen, iso()):
                ctx.stats["closed"] += 1
                ctx.decision(jid, "closed", "no longer listed on its source board")
                apps.close_for_job(db, jid)
    else:
        _save_cursor(db, 0)
    _update_uk_status(db)
    return status


def _process_user(db: Database, cfg: AppConfig, ctx: RunContext, uid: int, provider: Provider, prepare: bool,
                  time_left) -> tuple[dict, bool]:
    """Score, prepare and policy-approve for one user. Returns (stats, finished_within_budget)."""
    s = settings_mod.get_all(db, uid)
    ust = {"scored": 0, "prepared": 0, "policy_approved": 0}
    ai = AIService(db, provider, s["ai"], ctx.id, user_id=uid)   # disabled automatically for non-admin users
    try:
        resume = ensure_resume(db, cfg, user_id=uid)
    except ResumeError as e:
        resume = None
        ctx.errors.append(f"Resume (user {uid}): {e.message}")
    if not resume:
        return ust, True
    pversion = profile_version(resume, s)
    profile = candidate_profile(resume["structured"], resume["analysis"], s["candidate"])
    finished = True
    if time_left(5):
        budget_ai = s["ai"]["max_jobs_analyzed_per_run"]
        rows = db.all("SELECT j.id FROM jobs j WHERE j.status='open' AND j.is_sample=0 AND NOT EXISTS (SELECT 1 FROM "
                      "job_scores sc WHERE sc.job_id=j.id AND sc.user_id=? AND sc.profile_version=?) "
                      "ORDER BY j.first_discovered_at DESC", (uid, pversion))
        changed = {r["job_id"] for r in db.all("SELECT job_id FROM run_decisions WHERE run_id=? AND user_id IS NULL AND "
                                                "decision IN ('new','updated','reopened')", (ctx.id,))}
        ids = {r["id"] for r in rows}
        todo = [r["id"] for r in rows] + [j for j in changed if j not in ids]
        for jid in todo:
            if not time_left(4):
                finished = False
                ctx.errors.append("Time budget reached during scoring; remaining jobs will be scored next run.")
                break
            det = score_and_store(db, jid, profile, s, pversion, ai, ctx.id, use_ai=False, user_id=uid)
            ctx.stats["scored"] += 1
            ust["scored"] += 1
            # Second pass with AI only for promising jobs (saves cost; never for disqualified ones). Admin only.
            if budget_ai > 0 and ai.available and not det["disqualifiers"] and \
                    det["score"] >= s["min_match_score"] - 15 and jid in changed:
                score_and_store(db, jid, profile, s, pversion, ai, ctx.id, use_ai=True, user_id=uid)
                budget_ai -= 1
    else:
        finished = False

    if prepare and finished and time_left(10):
        limit = s["max_applications_per_run"]
        candidates = db.all(
            "SELECT j.id, sc.score FROM jobs j JOIN job_scores sc ON sc.id = (SELECT MAX(id) FROM job_scores "
            "WHERE job_id=j.id AND user_id=?) WHERE j.status='open' AND j.is_sample=0 AND sc.recommendation='apply' "
            "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id AND a.user_id=?) ORDER BY sc.score DESC",
            (uid, uid))
        for c in candidates:
            if ust["prepared"] >= limit:
                break
            if not time_left(6):
                finished = False
                break
            ctx.stats["shortlisted"] += 1
            ctx.decision(c["id"], "shortlisted", f"recommendation 'apply' with score {c['score']}", uid)
            try:
                prepare_application(db, c["id"], "agent", profile, s, ai, user_id=uid)
                ctx.stats["prepared"] += 1
                ust["prepared"] += 1
                ctx.decision(c["id"], "prepared", "draft package ready; waiting for your approval", uid)
            except (apps.WorkflowError, ResumeError) as e:
                ctx.errors.append(f"prepare job {c['id']} (user {uid}): {getattr(e, 'message', str(e))}")

    if prepare and s["auto_apply"]["enabled"] and s["auto_apply"]["acknowledged_at"]:
        approved = 0
        for row in db.all("SELECT a.id FROM applications a JOIN job_scores sc ON sc.id = (SELECT MAX(id) FROM "
                          "job_scores WHERE job_id=a.job_id AND user_id=a.user_id) WHERE a.user_id=? AND "
                          "a.status='awaiting_approval' AND a.is_sample=0 ORDER BY sc.score DESC", (uid,)):
            if approved >= s["auto_apply"]["max_per_run"] or not time_left(3):
                break
            app_row = apps.get_app(db, row["id"], user_id=uid)
            if apps.auto_apply_ineligibility(db, app_row, s) or apps.approval_gate_checks(db, app_row, s):
                continue
            try:
                apps.policy_approve(db, row["id"], user_id=uid)
                approved += 1
                ctx.decision(app_row["job_id"], "policy_approved",
                             "matches your auto-apply policy; queued for the auto-apply worker", uid)
            except apps.WorkflowError as e:
                ctx.errors.append(f"auto-apply approve #{row['id']}: {e.message}")
        ctx.stats["policy_approved"] += approved
        ust["policy_approved"] = approved
    return ust, finished


def _save_cursor(db: Database, idx: int) -> None:
    with db.transaction():
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "value_json=excluded.value_json, updated_at=excluded.updated_at", (CURSOR_KEY, dumps(idx), iso()))


def _update_uk_status(db: Database) -> None:
    names = load_register(db)
    if names is None:
        return
    rows = db.all("SELECT DISTINCT e.id, e.normalized_name, e.uk_sponsor_checked_at FROM employers e JOIN jobs j ON "
                  "j.employer_id=e.id WHERE j.country='GB'")
    for r in rows:
        status, routes = register_lookup(names, r["normalized_name"])
        with db.transaction():
            db.run("UPDATE employers SET uk_sponsor_status=?, uk_sponsor_routes=?, uk_sponsor_checked_at=? WHERE id=?",
                   (status, routes, iso(), r["id"]))


def verify_job(db: Database, job_id: int, http: HttpClient) -> str:
    """Re-check a single (shared) job before applying. Returns new status."""
    from .sources import AshbySource, GreenhouseSource, LeverSource
    listing = db.one("SELECT * FROM job_listings WHERE job_id=? ORDER BY last_seen_at DESC LIMIT 1", (job_id,))
    if not listing:
        return "unverified"
    adapters = {"greenhouse": GreenhouseSource(http), "lever": LeverSource(http), "ashby": AshbySource(http)}
    ad = adapters.get(listing["source"])
    result = ad.verify(listing["source_job_id"], None) if ad else None
    if result is None and listing["source"] == "ashby":
        board = listing["source_job_id"].split(":")[0]
        res = ad.fetch(board)
        if res.complete:
            result = any(j.source_job_id == listing["source_job_id"] for j in res.jobs)
    now = iso()
    with db.transaction():
        if result is True:
            db.run("UPDATE jobs SET status='open', last_verified_at=?, last_seen_at=? WHERE id=?", (now, now, job_id))
            new = "open"
        elif result is False:
            db.run("UPDATE jobs SET status='closed', last_verified_at=? WHERE id=?", (now, job_id))
            new = "closed"
        else:
            new = db.scalar("SELECT status FROM jobs WHERE id=?", (job_id,))
    if new == "closed":
        apps.close_for_job(db, job_id)
    audit(db, actor="agent", entity="job", entity_id=job_id, action="verify", detail={"result": new})
    return new
