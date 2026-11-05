"""Auto-apply worker: submits ONLY applications approved by the user's enabled auto-apply policy, on the employer's
own hosted form, with answers the user confirmed. Anything uncertain is handed back to the normal assisted flow.

Per application:
  eligibility (deterministic gates) -> policy approval -> PREFLIGHT (open form, detect ATS, CAPTCHA, questions)
  -> not automatable: revoke the policy approval (back to 'awaiting_approval') + audit the unknown questions
  -> automatable: start_application (consumes the single-use approval) -> fresh form -> fill -> submit
     -> confirmation detected: screenshot evidence + 'submitted'
     -> clicked but no confirmation / error after click: 'submission_uncertain' (never retried automatically)
     -> error before clicking submit: 'submission_failed' (before_submit)"""
from __future__ import annotations

import copy
import hashlib
import inspect
import logging
import random
import re
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import applications as apps
from .. import settings as settings_mod
from ..config import AppConfig
from ..db import Database, LockHeld, audit, iso, loads, named_lock
from ..resume import store as resume_store
from .captcha import detect_captcha
from .drivers import BaseDriver, DriverError, driver_for, host_in
from .matcher import Field, Matcher

log = logging.getLogger("jobagent.autoapply")
ACTOR = apps.AUTO_ACTOR
MAX_ATTEMPTS = 3
STAT_KEYS = ("considered", "not_eligible", "policy_approved", "handed_back", "submitted_confirmed", "uncertain",
             "failed", "fill_only_left_open", "dry_run")


class HandBack(Exception):
    def __init__(self, code: str, message: str, questions: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.questions = code, message, questions or []


class BeforeSubmit(Exception):
    pass


# ---------------------------------------------------------------------------- browser session

class BrowserSession:
    """One Chromium for the run, a fresh context per application. With `allow_hosts` (TESTS ONLY) requests to the
    mapped public hosts are served by a local fixture server and every other host is blocked (offline)."""

    def __init__(self, browser=None, headless: bool = True, allow_hosts: dict | None = None):
        self._pw = None
        self.allow_hosts = {k.lower(): v.rstrip("/") for k, v in (allow_hosts or {}).items()}
        if browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            browser = self._pw.chromium.launch(headless=headless)
            self._own = True
        else:
            self._own = False
        self.browser = browser

    def new_context(self, family: tuple[str, ...]):
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 1600}, locale="en-GB",
                                       accept_downloads=False)
        ctx.ja_blocked = []

        def main_nav(request) -> bool:
            try:
                return request.is_navigation_request() and request.frame.parent_frame is None
            except Exception:
                return False

        # Off-site main-frame navigations (incl. redirects, which route handlers do not see) are recorded and every
        # later step refuses to continue. Production uses no request interception at all (intercepted multipart
        # bodies can lose file contents).
        def on_request(request):
            host = (urlsplit(request.url).hostname or "").lower()
            if main_nav(request) and not host_in(host, family) and host not in ctx.ja_blocked:
                ctx.ja_blocked.append(host)
        ctx.on("request", on_request)
        if not self.allow_hosts:
            return ctx

        def handler(route, request):   # TESTS ONLY: serve mapped public hosts from the local fixture server
            url = request.url
            host = (urlsplit(url).hostname or "").lower()
            if main_nav(request) and not host_in(host, family):
                return route.abort("blockedbyclient")
            origin = self.allow_hosts.get(host)
            if not origin:
                return route.abort("blockedbyclient")
            def mapped(u):
                parts = urlsplit(u)
                return self.allow_hosts[(parts.hostname or "").lower()] + parts.path +                     (("?" + parts.query) if parts.query else "")
            try:
                resp = route.fetch(url=mapped(url), max_redirects=0)
                hops = 0
                # Redirects are followed here (the browser would send a fulfilled redirect to the real network).
                while 300 <= resp.status < 400 and hops < 5:
                    url = urljoin(url, resp.headers.get("location", ""))
                    nh = (urlsplit(url).hostname or "").lower()
                    if not host_in(nh, family) or nh not in self.allow_hosts:
                        if main_nav(request) and nh not in ctx.ja_blocked:
                            ctx.ja_blocked.append(nh)
                        return route.abort("blockedbyclient")
                    resp = ctx.request.get(mapped(url), max_redirects=0)
                    hops += 1
            except Exception:
                return route.abort("failed")
            return route.fulfill(response=resp)

        ctx.route("**/*", handler)
        return ctx

    def close(self) -> None:
        if self._own:
            try:
                self.browser.close()
            finally:
                if self._pw:
                    self._pw.stop()


# ---------------------------------------------------------------------------- helpers

def _resume_pdf(db: Database, app: dict) -> bytes:
    from ..resume import export
    rv = resume_store.get(db, app["resume_version_id"], user_id=app["user_id"]) if app["resume_version_id"] else None
    if not rv:
        raise HandBack("resume_unavailable", "The application has no resume version.")
    structured = rv.get("structured") or {}
    layout = loads(rv.get("layout_json"), None) if rv.get("layout_json") else None
    try:
        if "layout" in inspect.signature(export.export_pdf).parameters:
            data = export.export_pdf(structured, layout=layout)
        else:
            data = export.export_pdf(structured)
    except Exception as e:
        raise HandBack("resume_unavailable", f"Resume PDF could not be generated ({type(e).__name__}).") from e
    if not data or not data.startswith(b"%PDF"):
        raise HandBack("resume_unavailable", "Resume PDF could not be generated.")
    return data


def _file_stem(candidate: dict) -> str:
    parts = re.sub(r"[^A-Za-z ]", "", candidate.get("full_name") or "").split()
    name = "_".join([parts[0], parts[-1]] if len(parts) > 1 else parts) or "Candidate"
    return name


def _canonical_structured(db: Database, app: dict) -> dict:
    r = resume_store.canonical(db, user_id=app["user_id"])
    if r:
        return r.get("structured") or {}
    rv = resume_store.get(db, app["resume_version_id"], user_id=app["user_id"]) if app["resume_version_id"] else None
    return (rv or {}).get("structured") or {}


def _score(db: Database, job_id: int, user_id: int) -> float:
    return float(db.scalar("SELECT score FROM job_scores WHERE job_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                           (job_id, user_id)) or 0)


def _candidates(db: Database, user_id: int) -> list[dict]:
    rows = db.all(
        "SELECT a.* FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.user_id=? AND j.status='open' "
        "AND a.is_sample=0 AND a.external=0 AND a.status IN ('awaiting_approval','ready_to_apply') AND a.attempts < ?",
        (user_id, MAX_ATTEMPTS))
    out = []
    for a in rows:
        if a["status"] == "ready_to_apply":
            ap = apps.active_approval(db, a["id"], user_id=user_id)
            if not ap or ap.get("approved_by") != "policy":
                continue          # user approvals are the user's to use (assisted flow)
        handed = db.scalar("SELECT MAX(at) FROM audit_events WHERE entity='application' AND entity_id=? "
                           "AND action='autoapply_handback'", (str(a["id"]),))
        if handed and handed >= (a["updated_at"] or ""):
            continue              # handed back and not changed by the user since
        out.append(a)
    out.sort(key=lambda a: (-_score(db, a["job_id"], user_id), a["id"]))
    return out


def _save_evidence(db: Database, app_id: int, png: bytes, user_id: int) -> int:
    with db.transaction():
        fid = db.insert("INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, created_at, "
                        "user_id) VALUES ('submission_evidence', ?, 'image/png', ?, ?, ?, ?, ?)",
                        (f"autoapply-confirmation-app{app_id}.png", len(png), hashlib.sha256(png).hexdigest(),
                         db.blob(png), iso(), user_id))
        db.run("INSERT INTO uploads(application_id, file_id, created_at, user_id) VALUES (?,?,?,?)",
               (app_id, fid, iso(), user_id))
    return fid


def _body_text(page) -> str:
    try:
        return "\n".join(f.evaluate("() => document.body ? document.body.innerText : ''") or "" for f in page.frames)
    except Exception:
        return ""


# ---------------------------------------------------------------------------- the worker

class _Run:
    def __init__(self, db: Database, cfg: AppConfig, s: dict, *, dry_run: bool, headless: bool, session_factory,
                 pause, field_delay, confirm_timeout: float, assist_wait: float, ctx):
        self.db, self.cfg, self.s, self.pol = db, cfg, s, s["auto_apply"]
        self.uid: int | None = None
        self.dry_run, self.headless = dry_run, headless
        self.session_factory = session_factory
        self.session: BrowserSession | None = None
        self.pause, self.field_delay = pause, field_delay
        self.confirm_timeout, self.assist_wait = confirm_timeout, assist_wait
        self.ctx = ctx
        self.stats = {k: 0 for k in STAT_KEYS}
        self.stats["handed_back_reasons"] = {}
        self.results: list[dict] = []

    def use_user(self, uid: int, s: dict) -> None:
        """Switch to the next user's applications and auto-apply policy (the browser is shared)."""
        self.uid, self.s, self.pol = uid, s, s["auto_apply"]

    def browser(self) -> BrowserSession:
        if self.session is None:
            self.session = self.session_factory()
        return self.session

    # -------------------------------------------------------------- eligibility
    def eligible(self, app: dict) -> list[str]:
        if app["status"] == "ready_to_apply":
            return []
        return apps.approval_gate_checks(self.db, app, self.s) + apps.auto_apply_ineligibility(self.db, app, self.s)

    # -------------------------------------------------------------- one application
    def process(self, app: dict) -> dict:
        db = self.db
        app_id = app["id"]
        job = db.one("SELECT * FROM jobs WHERE id=?", (app["job_id"],))
        res = {"application_id": app_id, "job_id": job["id"], "outcome": None, "reason": ""}
        if app["status"] == "awaiting_approval" and not self.dry_run:
            try:
                apps.policy_approve(db, app_id, user_id=self.uid)
            except apps.WorkflowError as e:
                res.update(outcome="not_eligible", reason=e.message)
                return res
            self.stats["policy_approved"] += 1
        if not self.dry_run:
            db.run("UPDATE applications SET attempts=attempts+1 WHERE id=?", (app_id,))
        url = job["apply_url"]
        with ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="jobagent-aa-")))
            try:
                apps.verify_destination(url, job, db)
                driver = driver_for(url)
                if driver is None:
                    raise HandBack("unsupported_ats", f"'{urlsplit(url).hostname}' is not a supported ATS.")
                files, matcher = self._materials(app, job, tmp)
                family = driver.family(url)
                bctx = self.browser().new_context(family)
                stack.callback(lambda: _safe_close(bctx))
                page = bctx.new_page()
                frame, plan, captcha = self.preflight(page, bctx, driver, url, matcher, family, bool(matcher.cover))
            except HandBack as hb:
                return self._hand_back(app_id, res, hb)
            except Exception as e:  # unexpected before consuming the approval: hand back, never submit
                log.exception("preflight error")
                return self._hand_back(app_id, res, HandBack("preflight_error", f"Preflight failed: "
                                                                                 f"{type(e).__name__}"))
            if self.dry_run:
                return self._dry_fill(app_id, res, page, frame, driver, plan, files)
            if captcha and self.headless:
                return self._hand_back(app_id, res, HandBack(
                    "captcha", f"The form uses bot protection ({', '.join(captcha)}); it is never bypassed. "
                               "Apply yourself, or run the worker headed to have the form filled for you."))
            assist = bool(captcha) or self.pol["mode"] == "fill_only"
            if assist and self.headless:
                return self._hand_back(app_id, res, HandBack("fill_only_headless", "Fill-only mode needs a visible "
                                                                                   "browser (run with --headed)."))
            try:
                apps.start_application(db, app_id, ACTOR, user_id=self.uid)
            except apps.WorkflowError as e:
                res.update(outcome="refused", reason=e.message)
                if apps._get_app(db, app_id, self.uid)["status"] == "ready_to_apply":
                    apps.revoke_approval(db, app_id, ACTOR, reason=f"auto-apply could not start: {e.code}",
                                         user_id=self.uid)
                return res
            # ---- the approval is consumed: from here every exit records an outcome
            try:
                frame, plan = self._fresh_form(page, bctx, driver, url, matcher, family, bool(matcher.cover))
                driver.fill(frame, plan, files, self.field_delay)
            except Exception as e:
                reason = e.message if isinstance(e, HandBack) else str(e) or type(e).__name__
                return self._failed_before(app_id, res, f"Before submit: {reason}"[:400])
            if assist:
                return self._leave_open(app_id, res, page, captcha)
            return self._submit(app_id, res, page, bctx, frame, driver, family)

    def _materials(self, app: dict, job: dict, tmp: Path):
        cand = self.s["candidate"]
        stem = _file_stem(cand)
        resume_path = tmp / f"{stem}_Resume.pdf"
        resume_path.write_bytes(_resume_pdf(self.db, app))
        cl = self.db.one("SELECT body FROM cover_letters WHERE id=?", (app["cover_letter_id"],)) \
            if app["cover_letter_id"] else None
        cover = (cl or {}).get("body") or ""
        cover_path = tmp / f"{stem}_Cover_Letter.txt"
        cover_path.write_text(cover, encoding="utf-8")
        matcher = Matcher(cand, job, loads(app["answers_json"], []), _canonical_structured(self.db, app),
                          self.pol, cover)
        return {"resume": str(resume_path), "cover": str(cover_path)}, matcher

    def _open(self, page, bctx, driver: BaseDriver, url: str, family) -> object:
        target = driver.form_url(url)
        bctx.ja_blocked.clear()
        try:
            resp = page.goto(target, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            if bctx.ja_blocked:
                raise HandBack("destination_mismatch", "The form tried to send you to a different site "
                                                       f"({bctx.ja_blocked[0]}); refused.") from e
            raise HandBack("form_unreachable", f"The application page could not be opened ({type(e).__name__}).") from e
        if resp is not None and resp.status >= 400:
            raise HandBack("form_unreachable", f"The application page returned HTTP {resp.status}.")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self._guard(page, bctx, family)
        frame = driver.form_frame(page)
        self._guard(page, bctx, family)
        if frame is None:
            raise HandBack("form_not_found", "No application form was found on the page.")
        return frame

    @staticmethod
    def _guard(page, bctx, family) -> None:
        host = (urlsplit(page.url).hostname or "").lower()
        if bctx.ja_blocked or not host_in(host, family):
            raise HandBack("destination_mismatch", "The page left the verified application site "
                                                   f"({(bctx.ja_blocked or [host])[0]}); refused.")

    def _plan(self, frame, driver: BaseDriver, matcher: Matcher, has_cover: bool):
        driver.prepare_form(frame, has_cover)
        fields = driver.extract(frame)
        plan, blocking = matcher.plan(fields)
        # Prefer pasting the cover letter into a textarea over uploading it as a file.
        if any(d.action == "fill" and d.source == "cover_letter" for _, d in plan):
            plan = [(f, d) if not (d.action == "upload_cover" and not f.required) else
                    (f, type(d)("skip", reason="cover letter pasted instead")) for f, d in plan]
        if not any(d.action == "upload_resume" for _, d in plan):
            if any(f.kind == "file" for f in fields):
                blocking.append("Resume upload field not recognised")
        if not any(f.kind in ("email",) or d.rule == "email" for f, d in plan):
            blocking.append("No email field recognised")
        return fields, plan, blocking

    def preflight(self, page, bctx, driver, url, matcher, family, has_cover):
        frame = self._open(page, bctx, driver, url, family)
        captcha = detect_captcha(page)
        fields, plan, blocking = self._plan(frame, driver, matcher, has_cover)
        if blocking:
            raise HandBack("unknown_questions", f"{len(blocking)} required question(s) have no confirmed answer.",
                           blocking)
        return frame, plan, captcha

    def _fresh_form(self, page, bctx, driver, url, matcher, family, has_cover):
        frame = self._open(page, bctx, driver, url, family)
        fields, plan, blocking = self._plan(frame, driver, matcher, has_cover)
        if blocking:
            raise BeforeSubmit("The form changed and now has unanswered required questions: " + "; ".join(blocking))
        return frame, plan

    # -------------------------------------------------------------- outcomes
    def _hand_back(self, app_id: int, res: dict, hb: HandBack) -> dict:
        db = self.db
        if not self.dry_run:
            if apps._get_app(db, app_id, self.uid)["status"] == "ready_to_apply":
                apps.revoke_approval(db, app_id, ACTOR, reason=f"auto-apply handed back: {hb.code}",
                                     user_id=self.uid)
            audit(db, actor=ACTOR, entity="application", entity_id=app_id, action="autoapply_handback",
                  detail={"code": hb.code, "reason": hb.message[:300],
                          "unknown_questions": [q[:200] for q in hb.questions[:30]]})
        self.stats["handed_back"] += 1
        self.stats["handed_back_reasons"][hb.code] = self.stats["handed_back_reasons"].get(hb.code, 0) + 1
        res.update(outcome="handed_back", code=hb.code, reason=hb.message, unknown_questions=hb.questions)
        return res

    def _failed_before(self, app_id: int, res: dict, reason: str) -> dict:
        apps.record_outcome(self.db, app_id, "failed", ACTOR, user_id=self.uid, failure_stage="before_submit", reason=reason)
        self.stats["failed"] += 1
        res.update(outcome="failed_before_submit", reason=reason)
        return res

    def _dry_fill(self, app_id, res, page, frame, driver, plan, files) -> dict:
        filled = driver.fill(frame, plan, files, (0, 0))
        out = self.cfg.artifacts_dir / "autoapply"
        path = None
        try:
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"dryrun-app{app_id}-{iso()[:19].replace(':', '')}.png"
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            path = None
        self.stats["dry_run"] += 1
        res.update(outcome="dry_run", filled=filled, screenshot=str(path) if path else None,
                   captcha=detect_captcha(page), reason="Filled in a throwaway browser; nothing submitted.")
        return res

    def _leave_open(self, app_id, res, page, captcha) -> dict:
        why = f"bot protection ({', '.join(captcha)})" if captcha else "fill-only mode"
        audit(self.db, actor=ACTOR, entity="application", entity_id=app_id, action="autoapply_filled_for_review",
              detail={"why": why, "note": "Form filled; review it and click Submit yourself, then record the "
                                          "outcome in the dashboard."})
        self.stats["fill_only_left_open"] += 1
        res.update(outcome="left_open_for_user", reason=f"Filled; waiting for you to submit ({why}).")
        if self.assist_wait > 0:
            print(f"Application #{app_id}: the form is filled ({why}). Review it, click Submit yourself, then close "
                  "the tab. Record the outcome in the dashboard.")
            try:
                page.wait_for_event("close", timeout=self.assist_wait * 1000)
            except Exception:
                pass
        return res

    def _submit(self, app_id, res, page, bctx, frame, driver, family) -> dict:
        db = self.db
        captcha = detect_captcha(page)
        if captcha:
            return self._failed_before(app_id, res, f"Bot protection appeared before submit ({', '.join(captcha)}); "
                                                    "not bypassed.")
        btn = driver.find_submit(frame)
        if btn is None:
            return self._failed_before(app_id, res, "Submit button not found.")
        before_text, before_url = _body_text(page), page.url
        try:
            btn.click(timeout=10000)
        except Exception as e:
            # A failed click may still have triggered the submit: treat as unknown, never retry automatically.
            return self._uncertain(app_id, res, f"Error while clicking submit ({type(e).__name__}).")
        deadline = time.monotonic() + self.confirm_timeout
        conf = None
        while time.monotonic() < deadline:
            try:
                conf = driver.confirmation(page, before_text, before_url)
            except Exception:
                conf = None
            if conf:
                break
            if bctx.ja_blocked:
                break
            page.wait_for_timeout(500)
        if not conf:
            why = "navigation to another site was blocked" if bctx.ja_blocked else \
                f"no confirmation within {int(self.confirm_timeout)}s"
            return self._uncertain(app_id, res, f"Submitted but {why}; check your email/the portal.")
        try:
            png = page.screenshot(full_page=True)
            fid = _save_evidence(db, app_id, png, self.uid)
        except Exception:
            fid = None
        apps.record_outcome(db, app_id, "submitted", ACTOR, user_id=self.uid, confirmation_text=conf[:4000], file_id=fid)
        self.stats["submitted_confirmed"] += 1
        res.update(outcome="submitted", confirmation_text=conf, file_id=fid)
        return res

    def _uncertain(self, app_id, res, reason) -> dict:
        apps.record_outcome(self.db, app_id, "failed", ACTOR, user_id=self.uid, failure_stage="after_submit_unknown", reason=reason)
        self.stats["uncertain"] += 1
        res.update(outcome="submission_uncertain", reason=reason)
        return res


def _safe_close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


def run_autoapply(db: Database, cfg: AppConfig, *, dry_run: bool = False, headless: bool | None = None,
                  limit: int | None = None, pause: tuple[float, float] = (20, 60), browser=None,
                  allow_hosts: dict | None = None, field_delay: tuple[float, float] = (0.15, 0.6),
                  confirm_timeout: float = 45.0, assist_wait: float = 1800.0, trigger: str = "manual",
                  user_id: int | None = None) -> dict:
    """Process policy-approved applications. Returns {status, run_id, stats, results}.
    Multi-user: each approved user's OWN auto-apply policy decides; only users who enabled (and acknowledged) it
    are processed (`user_id` limits the run to one user). A dry run may preview the policy of the given user (or the
    admin) before it is enabled. `browser`/`allow_hosts` are test hooks (inject a Playwright browser; map public ATS hosts to a local
    fixture origin). They are deliberately not exposed in Settings or the CLI."""
    candidates_users = [user_id] if user_id is not None else settings_mod.approved_user_ids(db)
    plans: list[tuple[int, dict]] = []
    for uid in candidates_users:
        if db.scalar("SELECT status FROM users WHERE id=?", (uid,)) != "approved":
            continue
        s = settings_mod.get_all(db, uid)
        pol = s["auto_apply"]
        enabled = bool(pol.get("enabled") and pol.get("acknowledged_at"))
        if not enabled and dry_run and (user_id is not None or
                                        db.scalar("SELECT role FROM users WHERE id=?", (uid,)) == "admin"):
            # A dry run changes nothing, so it may preview the policy before the user enables it.
            s = copy.deepcopy(s)
            s["auto_apply"].update(enabled=True, acknowledged_at=pol.get("acknowledged_at") or "dry-run")
            enabled = True
        if enabled:
            plans.append((uid, s))
    if not plans:
        return {"status": "disabled", "run_id": None, "stats": {}, "results": [],
                "message": "Auto-apply is disabled (Settings -> Auto-apply)."}
    first_pol = plans[0][1]["auto_apply"]
    headless = first_pol.get("headless", True) if headless is None else bool(headless)
    from ..agent import RunContext
    try:
        lock = named_lock(db, "autoapply", ttl_minutes=120)
        lock.__enter__()
    except LockHeld as e:
        return {"status": "locked", "run_id": None, "stats": {}, "results": [], "message": str(e)}
    ctx = None
    run = None
    status = "failed"
    try:
        ctx = RunContext(db, "autoapply", trigger, cfg, settings_mod.get_all(db, None), user_id)
        run = _Run(db, cfg, plans[0][1], dry_run=dry_run, headless=headless,
                   session_factory=lambda: BrowserSession(browser, headless, allow_hosts),
                   pause=pause, field_delay=field_delay, confirm_timeout=confirm_timeout, assist_wait=assist_wait,
                   ctx=ctx)
        remaining = None if limit is None else max(0, int(limit))
        processed_total = 0
        for uid, s in plans:
            run.use_user(uid, s)
            budget = s["auto_apply"]["max_per_run"]
            if remaining is not None:
                budget = min(budget, remaining - processed_total)
            processed = 0
            for app in _candidates(db, uid):
                if processed >= budget:
                    break
                app = apps._get_app(db, app["id"], uid)
                run.stats["considered"] += 1
                reasons = run.eligible(app)
                if reasons:
                    run.stats["not_eligible"] += 1
                    ctx.decision(app["job_id"], "autoapply_not_eligible", "; ".join(reasons), uid)
                    continue
                if processed_total and pause and pause[1] > 0:
                    time.sleep(random.uniform(*pause))
                try:
                    res = run.process(app)
                except Exception as e:  # never let one application break the run
                    log.exception("auto-apply error")
                    ctx.errors.append(f"application {app['id']}: {type(e).__name__}: {str(e)[:200]}")
                    res = {"application_id": app["id"], "outcome": "error", "reason": type(e).__name__}
                run.results.append(res)
                if res.get("outcome") == "not_eligible":
                    run.stats["not_eligible"] += 1
                    continue
                processed += 1
                processed_total += 1
                ctx.decision(app["job_id"], f"autoapply_{res.get('outcome')}", str(res.get("reason") or "")[:500],
                             uid)
        status = "succeeded" if not ctx.errors else "partial"
        return {"status": status, "run_id": ctx.id, "stats": run.stats, "results": run.results,
                "dry_run": dry_run}
    except Exception as e:
        if ctx:
            ctx.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
        status = "failed"
        raise
    finally:
        if run and run.session:
            run.session.close()
        if ctx:
            if run:
                ctx.stats = dict(run.stats)
            try:
                ctx.finish(status)
            except Exception:
                log.exception("could not finish run record")
        lock.__exit__(None, None, None)
