"""FastAPI web app: login-only, multi-user dashboard with administrator approval.

Every route except /login, /signup (+ signed review links), /setup, /healthz, /static and the secret-protected cron
endpoint requires a valid server-side session of an APPROVED account; every state-changing request requires a CSRF
token. Templates are rendered with Jinja2 autoescaping.

Isolation: every query for private data is scoped to the signed-in user (request.state.session['user_id']); any ID
taken from a URL is looked up together with that user id, so another user's IDs return 404. Administrator-only routes
return 404 to everyone else. The administrator manages accounts but sees only account info and counts."""
from __future__ import annotations

import csv
import functools
import hashlib
import inspect
import json
import io
import logging
import re
import secrets
import traceback
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import agent as agent_mod
from .. import access
from .. import answers as answers_mod
from .. import audience
from .. import companion
from .. import helpdesk
from .. import onboarding as onboarding_mod
from .. import taxonomy
from .. import outreach
from .. import applications as apps
from .. import settings as settings_mod
from .. import users as users_mod
from ..ai import AIService, ai_access, make_provider
from ..config import AppConfig, acceptable_base_url, load_config, validate_config
from ..db import Database, DatabaseError, audit, dumps, iso, loads, verify_audit_chain
from ..http import HttpClient
from ..ingest import classify_pending
from ..mailer import Mailer, Message, make_mailer
from ..mailer import valid_email as mailer_valid_email
from ..resume import store as resume_store
from ..resume.parser import ResumeError
from ..resume.variants import ROLE_FAMILIES, render_text
from ..sponsorship import COUNTRY_GUIDANCE
from . import charts, queries, security

BASE_DIR = Path(__file__).parent
_log = logging.getLogger("jobagent.web")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
# paths a signed-in user who has not answered the onboarding questions may still open
ONBOARDING_OPEN = ("/welcome", "/logout", "/signed-out", "/static/", "/support", "/healthz", "/favicon.ico",
                   "/api/companion/")
PUBLIC_PATHS = ("/login", "/logout", "/signed-out", "/signup", "/signup/", "/setup", "/healthz", "/static/", "/api/cron/", "/api/companion/", "/favicon.ico")
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
EVIDENCE_TYPES = {b"\x89PNG": ("image/png", ".png"), b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
                  b"%PDF": ("application/pdf", ".pdf")}
USER_SECTIONS = ("candidate", "search", "weights", "limits", "auto_apply", "resume_style", "notifications", "password")
ADMIN_SECTIONS = ("sources", "ai", "schedule")


def create_app(cfg: AppConfig | None = None, mailer: Mailer | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="JobAgent", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.autoescape = True
    templates.env.filters["role_label"] = taxonomy.role_label
    templates.env.filters["industry_label"] = taxonomy.industry_label
    templates.env.filters["screen_label"] = lambda p: access.LABELS.get(p, p)
    signer = URLSafeTimedSerializer(cfg.secret_key or secrets.token_hex(32), salt="flash")
    state = {"migrated": False, "bg_run": None}
    lock = threading.Lock()
    app.state.mailer = mailer or make_mailer(cfg)
    app.state.http_factory = lambda: HttpClient(user_agent=cfg.user_agent)

    def get_db(request: Request) -> Database:
        if not hasattr(request.state, "db"):
            db = Database(cfg.database_url)
            with lock:
                if not state["migrated"]:
                    db.migrate()
                    try:
                        classify_pending(db)
                    except Exception:  # best-effort; jobs are classified again by the next agent run
                        pass
                    state["migrated"] = True
            request.state.db = db
        return request.state.db

    # ------------------------------------------------------------------ helpers
    def flash_cookie(resp: Response, kind: str, msg: str) -> Response:
        resp.set_cookie("ja_flash", signer.dumps({"k": kind, "m": msg[:500]}), max_age=60, httponly=True,
                        samesite="strict", secure=cfg.https, path="/")
        return resp

    def redirect(url: str, kind: str | None = None, msg: str | None = None) -> RedirectResponse:
        if not url.startswith("/") or url.startswith("//"):
            url = "/"
        resp = RedirectResponse(url, status_code=303)
        if kind and msg:
            flash_cookie(resp, kind, msg)
        return resp

    def _nav_badges(request: Request, sess: dict | None) -> dict:
        """Per-page extras for the layout: whether this user has AI, unread support replies / open tickets."""
        if not sess:
            return {"has_ai": False, "support_unread": 0, "support_open": 0}
        try:
            db = get_db(request)
            me = int(sess["user_id"])
            admin = sess.get("role") == "admin"
            us = settings_mod.get_all(db, me)
            hide = access.resolve(db, me, settings=us)["hidden"]   # admin-assigned / industry / automatic
            return {"nav_hide": hide, "career_summary": onboarding_mod.summary(db, me),
                    "has_ai": ai_access(db, me)["allowed"],
                    "support_unread": helpdesk.unread_for_user(db, me),
                    "support_open": int(db.scalar("SELECT COUNT(*) FROM support_tickets WHERE status='open'") or 0)
                    if admin else 0}
        except Exception:
            return {"has_ai": False, "support_unread": 0, "support_open": 0}

    def render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
        flash = None
        raw = request.cookies.get("ja_flash")
        if raw:
            try:
                flash = signer.loads(raw, max_age=120)
            except BadSignature:
                flash = None
        sess = getattr(request.state, "session", None)
        base = {"request": request, "user": sess["username"] if sess else None,
                "user_name": (sess.get("full_name") or sess["username"]) if sess else None,
                "is_admin": bool(sess and sess.get("role") == "admin"),
                "csrf": sess["csrf_token"] if sess else None, "flash": flash, "path": request.url.path,
                "labels": apps.STATUS_LABELS, "countries": settings_mod.COUNTRY_NAMES,
                "deployment": cfg.deployment, "today": date.today().isoformat(), **_nav_badges(request, sess)}
        resp = templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)
        if raw:
            resp.delete_cookie("ja_flash", path="/")
        return resp

    def not_found(request: Request, message: str = "Not found.") -> HTMLResponse:
        if getattr(request.state, "session", None):
            return render(request, "error.html", {"message": message}, 404)
        return PlainTextResponse("Not found", status_code=404)

    def actor(request: Request) -> str:
        return f"user:{request.state.session['username']}"

    def uid(request: Request) -> int:
        return int(request.state.session["user_id"])

    def is_admin(request: Request) -> bool:
        sess = request.state.session
        return bool(sess and sess.get("role") == "admin")

    async def form(request: Request) -> dict:
        data = await request.form()
        if not security.csrf_ok(request.state.session, data.get("csrf")):
            raise PermissionError("csrf")
        return data

    def safe_next(url: str | None) -> str:
        if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
            return "/"
        return url

    def client_ip(request: Request) -> str:
        ip = request.client.host if request.client else "unknown"
        if cfg.deployment == "vercel":
            ip = (request.headers.get("x-forwarded-for") or ip).split(",")[0].strip()
        return ip[:64]

    def base_url(request: Request) -> str | None:
        """Base URL for links in emails: JOBAGENT_PUBLIC_URL (or Vercel's production URL); the request's own URL
        only for local development (the Host header is attacker-controlled)."""
        if cfg.public_url:
            return acceptable_base_url(cfg.public_url)
        if cfg.deployment == "local":
            return acceptable_base_url(str(request.base_url))
        return None

    def ai_for(request: Request, s: dict) -> AIService:
        """AIService bound to the signed-in user; disabled for everyone except the administrator."""
        return AIService(get_db(request), make_provider(cfg), s["ai"], user_id=uid(request))

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        request.state.session = None
        try:
            if not path.startswith("/static/"):
                db = get_db(request)
                request.state.session = security.load_session(db, request.cookies.get(security.SESSION_COOKIE),
                                                              cfg.session_idle_minutes)
            public = any(path == p or (p.endswith("/") and path.startswith(p)) for p in PUBLIC_PATHS)
            if not public and request.state.session is None:
                if path.startswith("/api/") or request.method != "GET":
                    resp = JSONResponse({"error": "authentication required"}, status_code=401)
                else:
                    resp = RedirectResponse("/login?next=" + quote(path + (("?" + request.url.query)
                                                                           if request.url.query else "")), 303)
            elif (request.state.session is not None and not request.state.session.get("onboarded_at")
                  and not any(path == p or path.startswith(p if p.endswith("/") else p + "/") or path == p
                              for p in ONBOARDING_OPEN)):
                # new accounts answer the onboarding questions before anything else
                resp = RedirectResponse("/welcome", status_code=303)
            elif (request.state.session is not None and request.state.session.get("role") != "admin"
                  and any(path == p or path.startswith(p + "/") for p in access.SCREEN_PATHS)
                  and access.blocked(access.resolve(get_db(request), int(request.state.session["user_id"]))["hidden"],
                                     path)):
                # a screen the administrator turned off for this user: gone from the menu and refused here
                if request.method == "GET":
                    resp = RedirectResponse("/", status_code=303)
                    flash_cookie(resp, "error", "That screen is not enabled for your account. Ask the administrator "
                                                "under Help & Support if you need it.")
                else:
                    resp = PlainTextResponse("Not found", status_code=404)
            elif path.startswith("/admin") and not is_admin(request):
                # admin-only area: indistinguishable from a missing page for everyone else
                resp = PlainTextResponse("Not found", status_code=404) if request.method != "GET" else \
                    render(request, "error.html", {"message": "Page not found."}, 404)
            else:
                try:
                    resp = await call_next(request)
                except PermissionError:
                    resp = PlainTextResponse("Invalid or missing CSRF token. Reload the page and try again.",
                                             status_code=403)
        except DatabaseError:
            resp = PlainTextResponse("Database unavailable. Check DATABASE_URL / the database server and retry.",
                                     status_code=503)
        finally:
            db = getattr(request.state, "db", None)
            if db is not None:
                db.close()
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        if cfg.https:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        # Never leak stack traces or secrets to the client; log them server-side (Vercel runtime logs) with a
        # short reference the user can quote.
        ref = secrets.token_hex(4)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
        _log.error("Unhandled error ref=%s %s %s\n%s", ref, request.method, request.url.path, tb)
        try:
            db = Database(cfg.database_url)
            try:
                audit(db, actor="system", entity="error", entity_id=ref, action="unhandled_exception",
                      detail={"method": request.method, "path": request.url.path,
                              "error": f"{type(exc).__name__}: {str(exc)[:300]}", "trace_tail": tb[-1500:]})
            finally:
                db.close()
        except Exception:
            pass
        return PlainTextResponse(f"Something went wrong (reference {ref}). The error was logged; please retry.",
                                 status_code=500)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # ------------------------------------------------------------------ public routes
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    def _pre_csrf(request: Request) -> str:
        return request.cookies.get("ja_pre") or secrets.token_urlsafe(24)

    def _pre_response(request: Request, name: str, ctx: dict, status: int = 200):
        token = _pre_csrf(request)
        resp = render(request, name, {**ctx, "pre_csrf": token}, status)
        resp.set_cookie("ja_pre", token, max_age=1800, httponly=True, samesite="strict", secure=cfg.https, path="/")
        return resp

    def _pre_ok(request: Request, data) -> bool:
        supplied = data.get("pre_csrf")
        return bool(supplied) and secrets.compare_digest(str(supplied), request.cookies.get("ja_pre") or "")

    @app.get("/login")
    def login_page(request: Request, next: str = "/"):
        db = get_db(request)
        if security.user_count(db) == 0:
            return redirect("/setup")
        if request.state.session:
            return redirect(safe_next(next))
        return _pre_response(request, "login.html", {"next": safe_next(next), "error": None, "notice": None})

    @app.post("/login")
    async def login(request: Request):
        db = get_db(request)
        data = await request.form()
        if not _pre_ok(request, data):
            return PlainTextResponse("Session expired; reload the login page.", status_code=403)
        try:
            user = security.authenticate(db, data.get("username", ""), data.get("password", ""), client_ip(request))
        except security.AccountNotActive as e:
            # correct password, but the account is pending / rejected / suspended
            return _pre_response(request, "login.html", {"next": safe_next(data.get("next")), "error": None,
                                                         "notice": str(e), "account_status": e.status}, 403)
        except security.AuthError as e:
            return _pre_response(request, "login.html", {"next": safe_next(data.get("next")), "error": str(e),
                                                         "notice": None}, 401)
        token, _ = security.create_session(db, user["id"], cfg.session_absolute_hours)
        resp = redirect(safe_next(data.get("next")))
        resp.set_cookie(security.SESSION_COOKIE, token, httponly=True, samesite="strict", secure=cfg.https,
                        max_age=cfg.session_absolute_hours * 3600, path="/")
        resp.delete_cookie("ja_pre", path="/")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        if not request.state.session:          # already signed out (expired session, second tab, back button)
            resp = redirect("/signed-out")
            resp.delete_cookie(security.SESSION_COOKIE, path="/")
            return resp
        await form(request)
        db = get_db(request)
        security.destroy_session(db, request.cookies.get(security.SESSION_COOKIE))
        audit(db, actor=actor(request), entity="auth", entity_id=None, action="logout")
        resp = redirect("/signed-out")
        resp.delete_cookie(security.SESSION_COOKIE, path="/")
        return resp

    @app.get("/logout", response_class=HTMLResponse)
    def logout_confirm(request: Request):
        # Signing out changes state, so it needs the CSRF-protected POST; a plain link/GET only asks to confirm.
        if not request.state.session:
            return redirect("/signed-out")
        return render(request, "signed_out.html", {"confirm": True})

    @app.get("/signed-out", response_class=HTMLResponse)
    def signed_out(request: Request):
        if request.state.session:
            return redirect("/")
        return render(request, "signed_out.html", {"confirm": False})

    # ------------------------------------------------------------------ sign-up (public)
    @app.get("/signup")
    def signup_page(request: Request):
        db = get_db(request)
        if security.user_count(db) == 0:
            return redirect("/setup")
        if request.state.session:
            return redirect("/")
        return _pre_response(request, "signup.html", {"error": None, "form": {}})

    @app.post("/signup")
    async def signup(request: Request):
        db = get_db(request)
        if security.user_count(db) == 0:
            return redirect("/setup")
        data = await request.form()
        if not _pre_ok(request, data):
            return PlainTextResponse("Session expired; reload the sign-up page.", status_code=403)
        keep = {k: (data.get(k) or "")[:200] for k in ("full_name", "email", "username")}

        def again(msg: str, status: int = 400):
            return _pre_response(request, "signup.html", {"error": msg, "form": keep}, status)

        ip = client_ip(request)
        if security.signup_rate_limited(db, ip):
            return again("Too many sign-up attempts from your network. Try again in an hour.", 429)
        security.record_signup_attempt(db, ip)
        full_name = " ".join((data.get("full_name") or "").split())
        if not 1 <= len(full_name) <= 120:
            return again("Enter your name (up to 120 characters).")
        if data.get("password") != data.get("password2"):
            return again("Passwords do not match.")
        try:
            new_id = security.create_user(db, data.get("username", ""), data.get("password", ""),
                                          actor="signup", email=data.get("email", ""), full_name=full_name,
                                          role="user", status="pending", created_ip=ip,
                                          needs_onboarding=True)
        except security.EmailTaken:
            # Do not reveal that this email already has an account: show the same confirmation, create nothing.
            audit(db, actor="anonymous", entity="auth", entity_id=None, action="signup_duplicate_email")
            return render(request, "signup_done.html", {})
        except security.AuthError as e:
            return again(str(e))
        user = users_mod.get_user(db, new_id)
        try:
            tokens = users_mod.create_review_tokens(db, cfg.secret_key, new_id)
        except users_mod.AccountError:
            tokens = None
        users_mod.send_signup_request(db, app.state.mailer, admin_email=cfg.admin_email, base_url=base_url(request),
                                      user=user, tokens=tokens)
        resp = render(request, "signup_done.html", {})
        resp.delete_cookie("ja_pre", path="/")
        return resp

    @app.get("/signup/review/{token}")
    def signup_review_page(request: Request, token: str):
        db = get_db(request)
        try:
            info = users_mod.check_review_token(db, cfg.secret_key, token)
        except users_mod.AccountError as e:
            return render(request, "signup_review.html", {"error": str(e), "info": None, "done": None}, 400)
        return _pre_response(request, "signup_review.html", {"error": None, "info": info, "token": token,
                                                             "done": None})

    @app.post("/signup/review/{token}")
    async def signup_review(request: Request, token: str):
        db = get_db(request)
        data = await request.form()
        if not _pre_ok(request, data):
            return PlainTextResponse("This page expired; open the link from the email again.", status_code=403)
        try:
            res = users_mod.use_review_token(db, cfg.secret_key, token)
        except users_mod.AccountError as e:
            return render(request, "signup_review.html", {"error": str(e), "info": None, "done": None}, 400)
        users_mod.send_decision(db, app.state.mailer, base_url=base_url(request), user=res["user"],
                                action=res["action"])
        resp = render(request, "signup_review.html", {"error": None, "info": None, "done": res})
        resp.delete_cookie("ja_pre", path="/")
        return resp

    def _setup_allowed(request: Request) -> tuple[bool, bool]:
        """(allowed, needs_token). Setup only works while no user exists. Remote/hosted setups need the
        one-time JOBAGENT_SETUP_TOKEN; plain localhost does not."""
        db = get_db(request)
        if security.user_count(db) > 0:
            return False, False
        host = request.client.host if request.client else ""
        local = cfg.deployment == "local" and host in ("127.0.0.1", "::1", "localhost", "testclient")
        return True, not local

    @app.get("/setup")
    def setup_page(request: Request):
        allowed, needs_token = _setup_allowed(request)
        if not allowed:
            return redirect("/login")
        if needs_token and not cfg.setup_token:
            return PlainTextResponse("First-run setup is locked. Set JOBAGENT_SETUP_TOKEN in the environment, "
                                     "redeploy, then open /setup.", status_code=403)
        return _pre_response(request, "setup.html", {"needs_token": needs_token, "error": None})

    @app.post("/setup")
    async def setup(request: Request):
        allowed, needs_token = _setup_allowed(request)
        if not allowed:
            return redirect("/login")
        data = await request.form()
        if not _pre_ok(request, data):
            return PlainTextResponse("Session expired; reload.", status_code=403)
        if needs_token and not security.setup_token_ok(cfg.setup_token, data.get("setup_token")):
            return _pre_response(request, "setup.html", {"needs_token": True, "error": "Invalid setup token."}, 403)
        if data.get("password") != data.get("password2"):
            return _pre_response(request, "setup.html", {"needs_token": needs_token,
                                                          "error": "Passwords do not match."}, 400)
        db = get_db(request)
        try:
            security.create_user(db, data.get("username", ""), data.get("password", ""),
                                 email=(data.get("email") or cfg.admin_email or None),
                                 full_name=data.get("full_name") or None, created_ip=client_ip(request))
        except security.AuthError as e:
            return _pre_response(request, "setup.html", {"needs_token": needs_token, "error": str(e)}, 400)
        return redirect("/login", "ok", "Administrator account created. Sign in.")

    # ------------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        db = get_db(request)
        o = queries.overview(db, user_id=uid(request))
        spon_labels = queries.SPONSORSHIP
        ctx = {
            "o": o,
            "chart_weeks": charts.columns(o["chart_weeks"], "Applications submitted per week"),
            "chart_countries": charts.hbar([(settings_mod.COUNTRY_NAMES.get(k, k), v) for k, v in o["chart_countries"]],
                                           "Open jobs by country"),
            "chart_roles": charts.hbar(o["chart_roles"], "Open jobs by seniority"),
            "chart_sponsorship": charts.hbar(o["chart_sponsorship"], "Open jobs by sponsorship evidence", spon_labels),
            "chart_outcomes": charts.hbar(o["chart_outcomes"], "Application outcomes", apps.STATUS_LABELS),
            "resume": resume_store.canonical(db, user_id=uid(request)) is not None,
            "config_problems": validate_config(cfg) if is_admin(request) else [],
            "pending_signups": int(db.scalar("SELECT COUNT(*) FROM users WHERE status='pending'") or 0)
            if is_admin(request) else 0,
        }
        return render(request, "overview.html", ctx)

    # ------------------------------------------------------------------ jobs (shared list, private scores)
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):
        db = get_db(request)
        me = uid(request)
        f = queries.JobFilters.from_query(request.query_params)
        rows, total = queries.search_jobs(db, f, user_id=me, career=settings_mod.get(db, "career", me))
        selected_id = request.query_params.get("job")
        selected = None
        if selected_id and selected_id.isdigit():
            selected = queries.job_detail(db, int(selected_id), user_id=me)
        elif rows:
            selected = queries.job_detail(db, rows[0]["id"], user_id=me)
        qp = [(k, v) for k, v in request.query_params.multi_items() if k not in ("page", "job")]
        return render(request, "jobs.html", {
            "f": f, "rows": rows, "total": total, "selected": selected, "facets": queries.facet_counts(db),
            "pages": max(1, (total + f.per_page - 1) // f.per_page), "base_qs": urlencode(qp),
            "opts": {"date_posted": queries.DATE_POSTED, "experience": queries.EXPERIENCE_LABELS,
                     "job_type": queries.JOB_TYPES, "remote": queries.REMOTE_TYPES, "sponsorship": queries.SPONSORSHIP,
                     "rec": queries.RECOMMENDATIONS, "sort": queries.SORTS, "status": queries.APP_STATUSES},
            "target_countries": settings_mod.all_target_countries(settings_mod.get_all(db, me)) + ["IN"],
            "industry_opts": [(i.id, i.label) for i in taxonomy.INDUSTRIES],
            "industry_facets": {r["industry"]: r["n"] for r in db.all(
                "SELECT industry, COUNT(*) AS n FROM jobs WHERE status='open' AND is_sample=0 AND industry IS NOT NULL "
                "GROUP BY industry")},
        })

    @app.get("/jobs/export.csv")
    def jobs_csv(request: Request):
        db = get_db(request)
        f = queries.JobFilters.from_query(request.query_params)
        f.per_page, f.page = 100, 1
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "employer", "title", "country", "city", "remote", "employment_type", "seniority", "score",
                    "recommendation", "sponsorship", "salary_min", "salary_max", "currency", "posted", "status",
                    "application_status", "listing_url", "apply_url"])
        page = 1
        while True:
            f.page = page
            rows, total = queries.search_jobs(db, f, user_id=uid(request), career=settings_mod.get(db, "career", uid(request)))
            for r in rows:
                vals = [r["id"], r["employer"], r["title"], r["country"], r["city"], r["remote_type"],
                        r["employment_type"], r["seniority"], r["score"], r["recommendation"], r["sponsorship"],
                        r["salary_min"], r["salary_max"], r["salary_currency"],
                        r["published_at"] or r["first_discovered_at"], r["job_status"], r["app_status"] or "discovered",
                        r["canonical_url"], r["apply_url"]]
                # neutralise spreadsheet formula injection
                w.writerow([("'" + v) if isinstance(v, str) and v[:1] in "=+-@\t\r" else v for v in vals])
            if page * f.per_page >= total or page >= 200:
                break
            page += 1
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="jobs.csv"'})

    @app.get("/jobs/compare", response_class=HTMLResponse)
    def compare(request: Request):
        db = get_db(request)
        ids = [int(x) for x in request.query_params.getlist("id") if x.isdigit()][:4]
        jobs_ = [j for j in (queries.job_detail(db, i, user_id=uid(request)) for i in ids) if j]
        return render(request, "compare.html", {"jobs": jobs_})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        db = get_db(request)
        job = queries.job_detail(db, job_id, user_id=uid(request))
        if not job:
            return render(request, "error.html", {"message": "Job not found."}, 404)
        return render(request, "job.html", {"job": job,
                                            "guidance": COUNTRY_GUIDANCE.get(job["country"] or "", None)})

    @app.post("/jobs/{job_id}/bookmark")
    async def bookmark(request: Request, job_id: int):
        data = await form(request)
        db = get_db(request)
        try:
            queries.toggle_bookmark(db, job_id, user_id=uid(request))
        except LookupError:
            return not_found(request, "Job not found.")
        return redirect(safe_next(data.get("back")) or f"/jobs/{job_id}")

    @app.post("/jobs/{job_id}/verify")
    async def verify(request: Request, job_id: int):
        data = await form(request)
        db = get_db(request)
        if not db.one("SELECT id FROM jobs WHERE id=?", (job_id,)):
            return not_found(request, "Job not found.")
        http = HttpClient(user_agent=cfg.user_agent)
        try:
            status = agent_mod.verify_job(db, job_id, http)
        finally:
            http.close()
        return redirect(safe_next(data.get("back")) or f"/jobs/{job_id}", "ok",
                        f"Listing re-checked: {status}.")

    @app.post("/jobs/{job_id}/apply")
    async def apply_start(request: Request, job_id: int):
        """The 'Apply' button: verify listing, prepare the tailored package, go to the review screen."""
        await form(request)
        db = get_db(request)
        me = uid(request)
        if not db.one("SELECT id FROM jobs WHERE id=?", (job_id,)):
            return not_found(request, "Job not found.")
        existing = db.one("SELECT id, status FROM applications WHERE job_id=? AND user_id=?", (job_id, me))
        if existing and existing["status"] not in ("shortlisted", "submission_failed", "closed"):
            return redirect(f"/applications/{existing['id']}")
        http = HttpClient(user_agent=cfg.user_agent)
        try:
            status = agent_mod.verify_job(db, job_id, http)
        finally:
            http.close()
        if status != "open":
            return redirect(f"/jobs/{job_id}", "error", f"This job is {status} at the source; it can't be applied to.")
        s = settings_mod.get_all(db, me)
        resume = resume_store.canonical(db, user_id=me)
        if not resume:
            return redirect("/studio", "error", "Upload your resume first.")
        from ..matching import candidate_profile
        profile = candidate_profile(resume["structured"], resume["analysis"], s["candidate"])
        if not agent_mod.latest_score(db, job_id, user_id=me):
            agent_mod.score_and_store(db, job_id, profile, s, agent_mod.profile_version(resume, s), user_id=me)
        try:
            app_row = agent_mod.prepare_application(db, job_id, actor(request), profile, s, ai_for(request, s),
                                                    user_id=me)
        except (apps.WorkflowError, ResumeError) as e:
            return redirect(f"/jobs/{job_id}", "error", getattr(e, "message", str(e)))
        return redirect(f"/applications/{app_row['id']}", "ok", "Application package prepared. Review it below.")

    @app.post("/jobs/{job_id}/external")
    async def external(request: Request, job_id: int):
        data = await form(request)
        db = get_db(request)
        try:
            a = apps.mark_applied_externally(db, job_id, actor(request), data.get("applied_on", ""),
                                             data.get("note", ""), user_id=uid(request))
        except apps.WorkflowError as e:
            if e.code == "not_found":
                return not_found(request, "Job not found.")
            return redirect(f"/jobs/{job_id}", "error", e.message)
        return redirect(f"/applications/{a['id']}", "ok", "Recorded as applied outside JobAgent.")

    # ------------------------------------------------------------------ top MNCs
    @app.get("/mnc", response_class=HTMLResponse)
    def mnc_page(request: Request):
        db = get_db(request)
        companies = queries.mnc_overview(db, user_id=uid(request))
        sector = request.query_params.get("sector") or ""
        sectors = {"big_tech": "Big tech", "product": "Product companies", "investment_bank": "Banks & finance",
                   "consulting": "Consulting", "it_services": "IT services"}
        shown = [c for c in companies if not sector or c["sector"] == sector]
        f = queries.JobFilters.from_query(request.query_params)
        if not f.mnc:
            f.mnc = ["all"]
        f.per_page = 15
        top_rows, top_total = queries.search_jobs(db, f, user_id=uid(request),
                                                  career=settings_mod.get(db, "career", uid(request)))
        return render(request, "mnc.html", {"companies": shown, "sectors": sectors, "sector": sector,
                                            "top_rows": top_rows, "top_total": top_total,
                                            "mnc_settings": settings_mod.get(db, "mnc", None)})

    # ------------------------------------------------------------------ applications (private)
    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request):
        db = get_db(request)
        status = [s for s in request.query_params.getlist("status") if s in apps.STATUS_LABELS]
        rows = queries.applications_list(db, status or None, user_id=uid(request))
        groups = {}
        for r in rows:
            groups.setdefault(r["status"], []).append(r)
        return render(request, "applications.html", {"rows": rows, "groups": groups, "status": status})

    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application(request: Request, app_id: int):
        db = get_db(request)
        me = uid(request)
        a = apps.get_app(db, app_id, user_id=me)
        if not a:
            return render(request, "error.html", {"message": "Application not found."}, 404)
        job = queries.job_detail(db, a["job_id"], user_id=me)
        resume = resume_store.get(db, a["resume_version_id"], user_id=me) if a["resume_version_id"] else None
        cl = db.one("SELECT * FROM cover_letters WHERE id=? AND user_id=?", (a["cover_letter_id"], me)) \
            if a["cover_letter_id"] else None
        snap = apps.snapshot(db, a) if a["status"] in ("awaiting_approval",) else None
        events = db.all("SELECT * FROM audit_events WHERE entity='application' AND entity_id=? ORDER BY id",
                        (str(app_id),))
        for e in events:
            e["detail"] = loads(e["detail_json"], {})
        s = settings_mod.get_all(db, me)
        return render(request, "application.html", {
            "a": a, "job": job, "resume": resume, "cover": cl, "answers": loads(a["answers_json"], []),
            "missing": loads(a["missing_info_json"], []), "evidence": loads(a["submission_evidence_json"], None),
            "fingerprint": apps.fingerprint(snap) if snap else None, "snapshot": snap,
            "gate": apps.approval_gate_checks(db, a, s) if a["status"] == "awaiting_approval" else [],
            "approval": apps.active_approval(db, app_id, user_id=me), "events": events,
            "next_statuses": sorted(apps.TRANSITIONS.get(a["status"], set()) - {"submitted", "application_started",
                                                                                 "ready_to_apply", "awaiting_approval",
                                                                                 "submission_uncertain",
                                                                                 "submission_failed"}),
            "uploads": db.all("SELECT u.id, f.id AS file_id, f.original_name, f.created_at FROM uploads u JOIN files f "
                              "ON f.id=u.file_id WHERE u.application_id=? AND u.user_id=? AND f.user_id=?",
                              (app_id, me, me)),
        })

    async def _app_action(request: Request, app_id: int, fn, ok_msg: str):
        data = await form(request)
        db = get_db(request)
        if not apps.get_app(db, app_id, user_id=uid(request)):
            return not_found(request, "Application not found.")
        try:
            result = fn(db, data)
        except apps.WorkflowError as e:
            if e.code == "not_found":
                return not_found(request, "Application not found.")
            return redirect(f"/applications/{app_id}", "error", e.message)
        if isinstance(result, Response):
            return result
        return redirect(f"/applications/{app_id}", "ok", ok_msg)

    @app.post("/applications/{app_id}/approve")
    async def approve(request: Request, app_id: int):
        def fn(db, d):
            if d.get("confirm") != "yes":
                raise apps.WorkflowError("confirm", "Tick the confirmation box to approve.")
            return apps.approve(db, app_id, actor(request), d.get("fingerprint", ""), user_id=uid(request))
        return await _app_action(request, app_id, fn, "Approved. You can now open the official application page.")

    @app.post("/applications/{app_id}/revoke")
    async def revoke(request: Request, app_id: int):
        return await _app_action(request, app_id, lambda db, d: apps.revoke_approval(
            db, app_id, actor(request), user_id=uid(request)), "Approval revoked.")

    @app.post("/applications/{app_id}/start")
    async def start(request: Request, app_id: int):
        def fn(db, d):
            apps.start_application(db, app_id, actor(request), user_id=uid(request))
            return redirect(f"/applications/{app_id}?opened=1", "ok",
                            "Approval used. Open the employer's page, submit there, then record the result here.")
        return await _app_action(request, app_id, fn, "")

    @app.post("/applications/{app_id}/outcome")
    async def outcome(request: Request, app_id: int):
        data = await request.form()
        if not security.csrf_ok(request.state.session, data.get("csrf")):
            raise PermissionError("csrf")
        db = get_db(request)
        me = uid(request)
        if not apps.get_app(db, app_id, user_id=me):
            return not_found(request, "Application not found.")
        file_id = None
        upload = data.get("screenshot")
        if upload is not None and getattr(upload, "filename", ""):
            content = await upload.read(MAX_EVIDENCE_BYTES + 1)
            if len(content) > MAX_EVIDENCE_BYTES:
                return redirect(f"/applications/{app_id}", "error", "Screenshot must be under 5 MB.")
            kind = next((v for k, v in EVIDENCE_TYPES.items() if content.startswith(k)), None)
            if not kind:
                return redirect(f"/applications/{app_id}", "error", "Only PNG, JPEG or PDF files are accepted.")
            with db.transaction():
                file_id = db.insert("INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, "
                                    "created_at, user_id) VALUES ('submission_evidence', ?, ?, ?, ?, ?, ?, ?)",
                                    (f"confirmation{kind[1]}", kind[0], len(content),
                                     hashlib.sha256(content).hexdigest(), db.blob(content), iso(), me))
                db.run("INSERT INTO uploads(application_id, file_id, created_at, user_id) VALUES (?,?,?,?)",
                       (app_id, file_id, iso(), me))
        try:
            apps.record_outcome(db, app_id, data.get("outcome", ""), actor(request), user_id=me,
                                reference_number=data.get("reference_number", ""),
                                confirmation_text=data.get("confirmation_text", ""), file_id=file_id,
                                failure_stage=data.get("failure_stage", ""), reason=data.get("reason", ""))
        except apps.WorkflowError as e:
            return redirect(f"/applications/{app_id}", "error", e.message)
        return redirect(f"/applications/{app_id}", "ok", "Outcome recorded.")

    @app.get("/files/{file_id}")
    def download_file(request: Request, file_id: int):
        db = get_db(request)
        f = db.one("SELECT * FROM files WHERE id=? AND user_id=? AND kind IN ('submission_evidence','resume_original')",
                   (file_id, uid(request)))
        if not f:
            return PlainTextResponse("Not found", status_code=404)
        return Response(f["data"], media_type=f["content_type"],
                        headers={"Content-Disposition": f'attachment; filename="file-{file_id}"'})

    @app.post("/applications/{app_id}/applied")
    async def applied(request: Request, app_id: int):
        return await _app_action(request, app_id, lambda db, d: apps.mark_applied(
            db, app_id, actor(request), user_id=uid(request), reference_number=d.get("reference_number", ""),
            confirmation_text=d.get("confirmation_text", "")),
            "Marked as applied. Update it here when you hear back.")

    @app.post("/applications/{app_id}/retry")
    async def retry(request: Request, app_id: int):
        return await _app_action(request, app_id, lambda db, d: apps.retry_after_failure(
            db, app_id, actor(request), user_id=uid(request)), "Ready for a fresh review and approval.")

    @app.post("/applications/{app_id}/answers")
    async def answers(request: Request, app_id: int):
        def fn(db, d):
            qs, ans = d.getlist("question"), d.getlist("answer")
            return apps.update_answers(db, app_id, [{"question": q, "answer": a} for q, a in zip(qs, ans)],
                                       actor(request), user_id=uid(request))
        return await _app_action(request, app_id, fn, "Answers saved. Review again before approving.")

    @app.post("/applications/{app_id}/cover-letter")
    async def cover(request: Request, app_id: int):
        return await _app_action(request, app_id, lambda db, d: apps.update_cover_letter(
            db, app_id, d.get("body", ""), actor(request), user_id=uid(request)), "Cover letter saved as a new version.")

    @app.post("/applications/{app_id}/progress")
    async def progress(request: Request, app_id: int):
        def fn(db, d):
            v = d.get("row_version")
            return apps.update_progress(
                db, app_id, actor(request), d.get("to_status") or None, user_id=uid(request), notes=d.get("notes"),
                interview_at=d.get("interview_at"), follow_up_at=d.get("follow_up_at"),
                rejection_reason=d.get("rejection_reason"), offer_details=d.get("offer_details"),
                expected_version=int(v) if v and v.isdigit() else None)
        return await _app_action(request, app_id, fn, "Saved.")

    @app.post("/applications/{app_id}/correct")
    async def correct(request: Request, app_id: int):
        return await _app_action(request, app_id, lambda db, d: apps.correct_status(
            db, app_id, actor(request), d.get("to_status", ""), d.get("reason", ""), user_id=uid(request)),
            "Status corrected (audited).")

    @app.post("/applications/{app_id}/withdraw")
    async def withdraw(request: Request, app_id: int):
        return await _app_action(request, app_id, lambda db, d: apps.update_progress(
            db, app_id, actor(request), "withdrawn", user_id=uid(request)), "Withdrawn.")

    # ------------------------------------------------------------------ sponsorship & prep
    @app.get("/sponsorship", response_class=HTMLResponse)
    def sponsorship(request: Request):
        db = get_db(request)
        me = uid(request)
        rows = db.all(f"SELECT j.id, j.title, j.country, j.city, j.relocation_text, e.name AS employer, "
                      f"e.uk_sponsor_status, sa.classification, sa.evidence_json, sa.assessed_at, sc.score "
                      f"FROM jobs j JOIN employers e ON e.id=j.employer_id {queries.LATEST_SPONSOR} "
                      f"{queries.LATEST_SCORE} WHERE j.status='open' AND j.is_sample=0 AND "
                      f"COALESCE(sa.classification,'unknown') IN ('confirmed','likely','not_supported') "
                      f"ORDER BY CASE sa.classification WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 ELSE 2 END, "
                      f"sc.score DESC LIMIT 300", (me,))
        for r in rows:
            r["evidence"] = loads(r.pop("evidence_json"), [])
        reg = db.one("SELECT original_name, created_at FROM files WHERE kind='uk_sponsor_register' ORDER BY id DESC "
                     "LIMIT 1")
        s = settings_mod.get_all(db, me)
        return render(request, "sponsorship.html", {"rows": rows, "register": reg, "guidance": COUNTRY_GUIDANCE,
                                                    "targets": settings_mod.all_target_countries(s)})

    @app.get("/prep", response_class=HTMLResponse)
    def prep_page(request: Request):
        db = get_db(request)
        me = uid(request)
        plans = db.all("SELECT p.job_id, p.plan_json, j.title, e.name AS employer FROM prep_plans p JOIN jobs j ON "
                       "j.id=p.job_id JOIN employers e ON e.id=j.employer_id JOIN applications a ON a.job_id=j.id "
                       "AND a.user_id=p.user_id WHERE p.user_id=? AND j.is_sample=0 ORDER BY a.updated_at DESC LIMIT 30",
                       (me,))
        gap = {}
        for p in plans:
            p["plan"] = loads(p.pop("plan_json"), {})
            for item in p["plan"].get("study_plan", []):
                g = gap.setdefault(item["skill"], {"skill": item["skill"], "jobs": 0, "priority": item["priority"],
                                                   "resource": item.get("resource"), "topics": item["topics"]})
                g["jobs"] += 1
                if item["priority"] == "high":
                    g["priority"] = "high"
        progress = {r["skill"]: r for r in db.all("SELECT * FROM skill_progress WHERE user_id=?", (me,))}
        ordered = sorted(gap.values(), key=lambda g: (g["priority"] != "high", -g["jobs"]))
        return render(request, "prep.html", {"plans": plans, "gaps": ordered, "progress": progress})

    @app.post("/prep/skill")
    async def prep_skill(request: Request):
        data = await form(request)
        skill = (data.get("skill") or "").strip()[:60]
        status = data.get("status")
        if not skill or status not in ("not_started", "learning", "practising", "confident"):
            return redirect("/prep", "error", "Invalid skill update.")
        db = get_db(request)
        with db.transaction():
            db.run("INSERT INTO skill_progress(user_id, skill, status, notes, updated_at) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(user_id, skill) DO UPDATE SET status=excluded.status, notes=excluded.notes, "
                   "updated_at=excluded.updated_at", (uid(request), skill, status, (data.get("notes") or "")[:1000], iso()))
        return redirect("/prep", "ok", f"Progress for {skill} saved.")

    # ------------------------------------------------------------------ screening answers (auto-apply to-do list)
    @app.get("/answers", response_class=HTMLResponse)
    def answers_page(request: Request):
        db = get_db(request)
        me = uid(request)
        ov = answers_mod.overview(db, user_id=me)
        pol = settings_mod.get_all(db, me)["auto_apply"]
        return render(request, "answers.html", {"ov": ov, "pol": pol})

    @app.post("/answers")
    async def answers_save(request: Request):
        data = await form(request)
        qs, ans = data.getlist("question"), data.getlist("answer")
        items = [{"question": q[:300], "answer": a[:2000]} for q, a in zip(qs, ans) if q.strip() and a.strip()]
        if not items:
            return redirect("/answers", "error", "Type an answer before saving.")
        res = answers_mod.save(get_db(request), user_id=uid(request), actor=actor(request), items=items)
        msg = f"Saved {res['saved']} answer(s)."
        if res["requeued"]:
            msg += f" {res['requeued']} handed-back application(s) will be retried on the next auto-apply run."
        return redirect("/answers", "ok", msg)

    @app.post("/answers/delete")
    async def answers_delete(request: Request):
        data = await form(request)
        ok = answers_mod.delete(get_db(request), user_id=uid(request), actor=actor(request),
                                question=(data.get("question") or "")[:300])
        return redirect("/answers#library", "ok" if ok else "error", "Answer removed." if ok else "Answer not found.")

    @app.post("/answers/consent")
    async def answers_consent(request: Request):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        pol = dict(settings_mod.get_all(db, me)["auto_apply"])
        pol["accept_privacy_notices"] = data.get("accept") == "1"
        settings_mod.set_value(db, "auto_apply", pol, actor(request), user_id=me)
        n = answers_mod.requeue_handed_back(db, user_id=me, actor=actor(request)) if pol["accept_privacy_notices"] else 0
        return redirect("/answers", "ok", ("Required privacy/data-processing boxes will be ticked for you."
                                           + (f" {n} application(s) re-queued." if n else ""))
                        if pol["accept_privacy_notices"] else "Privacy boxes will be left for you to tick.")

    # ------------------------------------------------------------------ recruiter outreach
    def _current_title(db: Database, me: int) -> str | None:
        r = resume_store.canonical(db, user_id=me)
        exp = ((r or {}).get("structured") or {}).get("experience") or []
        return (exp[0].get("title") if exp and isinstance(exp[0], dict) else None) or None

    @app.get("/recruiters", response_class=HTMLResponse)
    def recruiters_page(request: Request):
        db = get_db(request)
        me = uid(request)
        q = (request.query_params.get("q") or "").strip()[:80]
        try:
            min_score = max(0, min(100, int(request.query_params.get("min_score") or 60)))
        except ValueError:
            min_score = 60
        show = request.query_params.get("show") or "with_contacts"
        board = outreach.hiring_board(db, user_id=me, min_score=min_score, company=q)
        with_contacts = [b for b in board if b["contacts"]]
        rows = with_contacts if show != "all" else board
        return render(request, "recruiters.html", {
            "rows": rows[:150], "total": len(board), "with_contacts": len(with_contacts), "q": q,
            "min_score": min_score, "show": show, "history": outreach.history(db, user_id=me, limit=30),
            "server_mail": app.state.mailer.provider.name != "console"})

    @app.post("/recruiters/contacts")
    async def recruiters_add(request: Request):
        data = await form(request)
        db = get_db(request)
        job_id = data.get("job_id")
        job_id = int(job_id) if job_id and str(job_id).isdigit() else None
        if job_id and not db.scalar("SELECT 1 FROM jobs WHERE id=?", (job_id,)):
            job_id = None
        try:
            outreach.add_contact(db, user_id=uid(request), company=data.get("company") or "",
                                 email=data.get("email") or "", name=data.get("name") or "",
                                 role_title=data.get("role_title") or "", profile_url=data.get("profile_url") or "",
                                 job_id=job_id, notes=data.get("notes") or "")
        except ValueError as e:
            return redirect(safe_next(data.get("next") or "/recruiters"), "error", str(e))
        return redirect(safe_next(data.get("next") or "/recruiters"), "ok", "Contact saved.")

    @app.post("/recruiters/contacts/{cid}/delete")
    async def recruiters_delete(request: Request, cid: int):
        await form(request)
        ok = outreach.delete_contact(get_db(request), user_id=uid(request), contact_id=cid)
        return redirect("/recruiters", "ok" if ok else "error", "Contact removed." if ok else "Contact not found.")

    @app.get("/recruiters/compose", response_class=HTMLResponse)
    def recruiters_compose(request: Request):
        db = get_db(request)
        me = uid(request)
        try:
            job_id = int(request.query_params.get("job") or 0)
        except ValueError:
            job_id = 0
        job = outreach.job_for(db, job_id, user_id=me) if job_id else None
        if not job:
            return not_found(request, "That job is not open any more.")
        to = (request.query_params.get("to") or "").strip().lower()[:254]
        name = (request.query_params.get("name") or "").strip()[:120]
        s = settings_mod.get_all(db, me)
        subject, body = outreach.draft(job, s["candidate"], name or None, _current_title(db, me))
        return render(request, "recruiter_compose.html", {
            "job": job, "to": to, "to_name": name, "subject": subject, "body": body,
            "previous": outreach.previous(db, user_id=me, job_id=job_id, to_addr=to) if to else 0,
            "server_mail": app.state.mailer.provider.name != "console", "candidate": s["candidate"],
            "sent_today": outreach.server_sent_today(db, user_id=me), "cap": outreach.SERVER_DAILY_CAP})

    @app.post("/recruiters/send")
    async def recruiters_send(request: Request):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        try:
            job_id = int(data.get("job_id") or 0)
        except ValueError:
            job_id = 0
        job = outreach.job_for(db, job_id, user_id=me) if job_id else None
        to = (data.get("to") or "").strip().lower()
        to_name = (data.get("to_name") or "").strip()[:120]
        subject = (data.get("subject") or "").strip()[:200]
        body = (data.get("body") or "").replace("\r\n", "\n").strip()[:8000]
        channel = data.get("channel")
        back = "/recruiters/compose?" + urlencode({"job": job_id, "to": to, "name": to_name})
        if not job:
            return redirect("/recruiters", "error", "That job is not open any more.")
        if not mailer_valid_email(to) or not subject or not body or channel not in ("gmail", "mail_app", "server"):
            return redirect(back, "error", "Add a valid recipient, a subject and a message.")
        if outreach.previous(db, user_id=me, job_id=job_id, to_addr=to) and data.get("confirm_repeat") != "1":
            return redirect(back, "error", "You already contacted this person about this job. Tick the "
                                           "confirmation box to send another message.")
        common = dict(user_id=me, job_id=job_id, to_addr=to, to_name=to_name, company=job["employer"],
                      subject=subject, body=body)
        if channel == "server":
            mailer = app.state.mailer
            if mailer.provider.name == "console":
                return redirect(back, "error", "Server email is not configured; use Gmail or your mail app.")
            if outreach.server_sent_today(db, user_id=me) >= outreach.SERVER_DAILY_CAP:
                return redirect(back, "error", f"Daily limit of {outreach.SERVER_DAILY_CAP} emails reached.")
            reply_to = settings_mod.get_all(db, me)["candidate"].get("email")
            res = mailer.send(db, Message(to=to, subject=subject, text=body, kind="outreach", related_user_id=me,
                                          reply_to=reply_to if mailer_valid_email(reply_to) else None))
            ok = res["status"] == "sent"
            outreach.log(db, channel="server", status="sent" if ok else "failed", error=res.get("error"), **common)
            audit(db, actor=actor(request), entity="job", entity_id=job_id, action="outreach_sent" if ok else
                  "outreach_failed", detail={"to_domain": to.split("@")[-1], "channel": "server"})
            return redirect("/recruiters", "ok" if ok else "error",
                            f"Email sent to {to}." if ok else f"Email could not be sent: {res.get('error')}")
        outreach.log(db, channel=channel, status="opened", **common)
        audit(db, actor=actor(request), entity="job", entity_id=job_id, action="outreach_drafted",
              detail={"to_domain": to.split("@")[-1], "channel": channel})
        if channel == "gmail":
            return RedirectResponse(outreach.gmail_url(to, subject, body), status_code=303)
        # mail app: a redirect to mailto: is unreliable across browsers, so show the edited draft with a link the
        # person clicks (the click is what lets the browser hand over to the mail app)
        s = settings_mod.get_all(db, me)
        return render(request, "recruiter_compose.html", {
            "job": job, "to": to, "to_name": to_name, "subject": subject, "body": body,
            "previous": outreach.previous(db, user_id=me, job_id=job_id, to_addr=to),
            "server_mail": app.state.mailer.provider.name != "console", "candidate": s["candidate"],
            "sent_today": outreach.server_sent_today(db, user_id=me), "cap": outreach.SERVER_DAILY_CAP,
            "mailto": outreach.mailto_url(to, subject, body)})

    # ------------------------------------------------------------------ resume studio (private)
    @app.get("/studio", response_class=HTMLResponse)
    def studio(request: Request):
        db = get_db(request)
        me = uid(request)
        versions = resume_store.list_versions(db, user_id=me)
        sel = request.query_params.get("v")
        canonical = resume_store.canonical(db, user_id=me)
        if sel and sel.isdigit():
            current = resume_store.get(db, int(sel), user_id=me)
            if current is None:
                return render(request, "error.html", {"message": "Resume version not found."}, 404)
        else:
            current = canonical
        diff = None
        if current and current["kind"] == "variant" and canonical:
            import difflib
            diff = list(difflib.unified_diff(render_text(canonical["structured"]).splitlines(),
                                             render_text(current["structured"]).splitlines(), "canonical",
                                             current["label"], lineterm="", n=1))
        letters_ = db.all("SELECT c.id, c.version, c.method, c.status, c.created_at, j.title, e.name AS employer "
                          "FROM cover_letters c JOIN jobs j ON j.id=c.job_id JOIN employers e ON e.id=j.employer_id "
                          "WHERE c.user_id=? ORDER BY c.id DESC LIMIT 30", (me,))
        return render(request, "studio.html", {"versions": versions, "current": current, "canonical": canonical,
                                               "diff": diff, "families": ROLE_FAMILIES, "letters": letters_,
                                               "text": render_text(current["structured"]) if current else None})

    @app.post("/studio/upload")
    async def studio_upload(request: Request):
        data = await request.form()
        if not security.csrf_ok(request.state.session, data.get("csrf")):
            raise PermissionError("csrf")
        up = data.get("resume")
        if up is None or not getattr(up, "filename", ""):
            return redirect("/studio", "error", "Choose a file to upload.")
        content = await up.read(resume_store.MAX_UPLOAD_BYTES + 1)
        db = get_db(request)
        try:
            v = resume_store.import_bytes(db, content, up.filename, actor=actor(request), user_id=uid(request))
        except ResumeError as e:
            return redirect("/studio", "error", e.message)
        msg = "Resume imported." + ("" if v["is_canonical"] else
                                    " It is NOT canonical yet; use 'Make canonical' if it should replace the current one.")
        return redirect(f"/studio?v={v['id']}", "ok", msg)

    @app.post("/studio/variant")
    async def studio_variant(request: Request):
        data = await form(request)
        db = get_db(request)
        s = settings_mod.get_all(db, uid(request))
        try:
            v = resume_store.create_variant(db, data.get("role_family", ""), data.get("country") or None,
                                            actor=actor(request), ai=ai_for(request, s), user_id=uid(request))
        except (ResumeError, ValueError) as e:
            return redirect("/studio", "error", getattr(e, "message", str(e)))
        return redirect(f"/studio?v={v['id']}", "ok", "Variant generated. Review the changes before using it.")

    def _owned_resume(request: Request, rid: int) -> bool:
        return resume_store.get(get_db(request), rid, user_id=uid(request)) is not None

    @app.post("/studio/{rid}/summary")
    async def studio_summary(request: Request, rid: int):
        data = await form(request)
        db = get_db(request)
        if not _owned_resume(request, rid):
            return not_found(request, "Resume version not found.")
        try:
            resume_store.update_variant_summary(db, rid, data.get("summary", ""), actor(request), user_id=uid(request))
        except (ResumeError, LookupError) as e:
            return redirect(f"/studio?v={rid}", "error", getattr(e, "message", str(e)))
        return redirect(f"/studio?v={rid}", "ok", "Summary updated.")

    @app.post("/studio/{rid}/status")
    async def studio_status(request: Request, rid: int):
        data = await form(request)
        db = get_db(request)
        if not _owned_resume(request, rid):
            return not_found(request, "Resume version not found.")
        try:
            resume_store.set_status(db, rid, data.get("status", ""), actor(request), user_id=uid(request))
        except (ValueError, LookupError) as e:
            return redirect(f"/studio?v={rid}", "error", str(e))
        return redirect(f"/studio?v={rid}", "ok", "Status updated.")

    @app.post("/studio/{rid}/canonical")
    async def studio_canonical(request: Request, rid: int):
        data = await form(request)
        db = get_db(request)
        if not _owned_resume(request, rid):
            return not_found(request, "Resume version not found.")
        try:
            resume_store.set_canonical(db, rid, actor(request), data.get("confirm") == "yes", user_id=uid(request))
        except (PermissionError, LookupError, ValueError) as e:
            return redirect(f"/studio?v={rid}", "error", str(e))
        return redirect(f"/studio?v={rid}", "ok", "Canonical resume changed.")

    @app.post("/studio/{rid}/reanalyze")
    async def studio_reanalyze(request: Request, rid: int):
        await form(request)
        db = get_db(request)
        if not _owned_resume(request, rid):
            return not_found(request, "Resume version not found.")
        try:
            resume_store.reanalyze(db, rid, actor(request), user_id=uid(request))
        except LookupError as e:
            return redirect("/studio", "error", str(e))
        return redirect(f"/studio?v={rid}", "ok", "Analysis refreshed.")

    @app.get("/studio/{rid}/download.{fmt}")
    def studio_download(request: Request, rid: int, fmt: str):
        db = get_db(request)
        v = resume_store.get(db, rid, user_id=uid(request))
        if not v:
            return PlainTextResponse("Not found", status_code=404)
        if fmt == "original":
            f = resume_store.original_file(db, rid, user_id=uid(request))
            if not f:
                return PlainTextResponse("Not found", status_code=404)
            return Response(f["data"], media_type=f["content_type"],
                            headers={"Content-Disposition": f'attachment; filename="original-{rid}"'})
        if fmt not in ("pdf", "docx"):
            return PlainTextResponse("Not found", status_code=404)
        data = resume_store.export_version(v, fmt)   # uses the saved one-page layout
        media = "application/pdf" if fmt == "pdf" else \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", resume_store.download_filename(v, fmt))
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.get("/letters/{cid}", response_class=HTMLResponse)
    def letter(request: Request, cid: int):
        db = get_db(request)
        c = db.one("SELECT c.*, j.title, e.name AS employer FROM cover_letters c JOIN jobs j ON j.id=c.job_id JOIN "
                   "employers e ON e.id=j.employer_id WHERE c.id=? AND c.user_id=?", (cid, uid(request)))
        if not c:
            return render(request, "error.html", {"message": "Cover letter not found."}, 404)
        return render(request, "letter.html", {"c": c})

    # ------------------------------------------------------------------ runs & audit
    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        db = get_db(request)
        rows = queries.visible_runs(db, user_id=uid(request), is_admin=is_admin(request))
        for r in rows:
            r["stats"] = loads(r["stats_json"], {})
            r["errors"] = loads(r["errors_json"], []) if is_admin(request) or r.get("user_id") == uid(request) else []
        running = state["bg_run"] is not None and state["bg_run"].is_alive()
        return render(request, "runs.html", {"rows": rows, "running": running})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str):
        db = get_db(request)
        me = uid(request)
        r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not r or not (is_admin(request) or r.get("user_id") == me):
            return render(request, "error.html", {"message": "Run not found."}, 404)
        r["stats"], r["errors"] = loads(r["stats_json"], {}), loads(r["errors_json"], [])
        r["sources"], r["config"] = loads(r["sources_json"], []), loads(r["config_json"], {})
        # shared discovery decisions + this user's own decisions (never another user's)
        decisions = db.all("SELECT d.*, j.title, e.name AS employer FROM run_decisions d LEFT JOIN jobs j ON j.id=d.job_id "
                           "LEFT JOIN employers e ON e.id=j.employer_id WHERE d.run_id=? AND (d.user_id IS NULL OR "
                           "d.user_id=?) ORDER BY d.id LIMIT 1000", (run_id, me))
        usage = db.all("SELECT model, purpose, COUNT(*) AS calls, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, "
                       "SUM(cost_usd) AS cost, SUM(cached) AS cached FROM ai_usage WHERE run_id=? AND user_id=? "
                       "GROUP BY model, purpose", (run_id, me))
        return render(request, "run.html", {"r": r, "decisions": decisions, "usage": usage})

    def _run_now(trigger: str, budget: float | None, *, user_ids: list[int] | None = None,
                 discover: bool = True, triggered_by: int | None = None):
        db = Database(cfg.database_url)
        try:
            return agent_mod.run_agent(db, cfg, trigger=trigger, provider=make_provider(cfg), time_budget_s=budget,
                                       user_ids=user_ids, discover=discover, triggered_by=triggered_by)
        finally:
            db.close()

    @app.post("/runs/start")
    async def run_start(request: Request):
        """Admin: full run (discover once, then score/prepare for every approved user). Other users: score and
        prepare THEIR jobs only (discovery is an installation-wide admin/cron operation)."""
        await form(request)
        me = uid(request)
        audit(get_db(request), actor=actor(request), entity="run", entity_id=None, action="manual_start")
        kwargs = {"triggered_by": me} if is_admin(request) else \
            {"triggered_by": me, "user_ids": [me], "discover": False}
        if cfg.deployment == "vercel":
            res = _run_now("manual", cfg.run_budget_seconds, **kwargs)
            return redirect(f"/runs/{res['run_id']}", "ok" if res["status"] != "failed" else "error",
                            f"Run {res['status']}." + (" Click Run agent now again to continue with the remaining sources/jobs."
                                                       if res["status"] == "partial" else ""))
        if state["bg_run"] is not None and state["bg_run"].is_alive():
            return redirect("/runs", "error", "A run is already in progress.")
        t = threading.Thread(target=lambda: _run_now("manual", None, **kwargs), daemon=True)
        t.start()
        state["bg_run"] = t
        time.sleep(0.5)
        return redirect("/runs", "ok", "Run started. Refresh this page to follow progress.")

    @app.get("/api/cron/run")
    def cron(request: Request):
        auth = request.headers.get("authorization", "")
        if not cfg.cron_secret or not secrets.compare_digest(auth, f"Bearer {cfg.cron_secret}"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        res = _run_now("scheduled", cfg.run_budget_seconds if cfg.deployment == "vercel" else None)
        return {"run_id": res["run_id"], "status": res["status"]}

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        db = get_db(request)
        entity = request.query_params.get("entity") or ""
        if entity not in ("application", "job", "resume", "settings", "auth", "run", "admin"):
            entity = ""
        sess = request.state.session
        rows = queries.visible_audit(db, user_id=uid(request), username=sess["username"], is_admin=is_admin(request),
                                     entity=entity)
        ok, bad = verify_audit_chain(db)
        return render(request, "audit.html", {"rows": rows, "chain_ok": ok, "bad_id": bad, "entity": entity})

    # ------------------------------------------------------------------ settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        db = get_db(request)
        me = uid(request)
        s = settings_mod.get_all(db, me)
        usage_month = db.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage WHERE at >= ?",
                                (date.today().replace(day=1).isoformat(),)) if is_admin(request) else 0
        return render(request, "settings.html", {
            "s": s, "json": {k: dumps(v) for k, v in s.items()}, "families": ROLE_FAMILIES,
            "ai_provider": cfg.ai_provider, "ai_key_set": bool(cfg.openai_api_key or cfg.anthropic_api_key),
            "ai_month": round(float(usage_month or 0), 4),
            "config_problems": validate_config(cfg) if is_admin(request) else [],
            "db_kind": "PostgreSQL" if cfg.database_url.startswith("postgres") else "SQLite",
            **_salary_preview(db, s),
        })

    def _salary_preview(db: Database, s: dict) -> dict:
        from .. import fx
        es = (s.get("candidate") or {}).get("expected_salary") or {}
        rates = fx.rates(db)
        examples = []
        if es.get("amount"):
            targets = [c for c in settings_mod.all_target_countries(s)][:6]
            seen = set()
            for c in targets:
                r = fx.expected_for_job(es, {"country": c}, rates)
                if r and r["currency"] not in seen and r["currency"] != es.get("currency"):
                    seen.add(r["currency"])
                    examples.append(f"{fx.fmt(r['amount'], r['currency'])}/year in {settings_mod.COUNTRY_NAMES.get(c, c)}")
        return {"currencies": settings_mod.CURRENCIES, "salary_examples": examples[:4], "fx_date": rates.get("date")}

    @app.post("/settings")
    async def settings_save(request: Request):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        section = data.get("section", "")
        if section in ADMIN_SECTIONS and not is_admin(request):
            return PlainTextResponse("Not found", status_code=404)
        s = settings_mod.get_all(db, me)
        who = actor(request)

        def put(key, value):
            settings_mod.set_value(db, key, value, who, user_id=me)

        def lines(name):
            return [x.strip() for x in (data.get(name) or "").replace(",", "\n").splitlines() if x.strip()]
        try:
            if section == "candidate":
                cand = dict(s["candidate"])
                for k in ("full_name", "email", "phone", "linkedin_url", "github_url", "portfolio_url",
                          "current_location", "nationality", "notice_period"):
                    cand[k] = data.get(k) or None
                amount = (data.get("expected_amount") or "").strip()
                if amount:
                    from ..fx import parse_amount
                    if parse_amount(amount) is None:
                        raise settings_mod.SettingsError("Expected salary must be a number, e.g. 45000 or 15,00,000.")
                    cand["expected_salary"] = {"amount": amount, "currency": data.get("expected_currency") or "EUR",
                                               "period": data.get("expected_period") or "year"}
                else:
                    cand["expected_salary"], cand["salary_expectation"] = None, None
                cand["requires_sponsorship_outside"] = data.get("requires_sponsorship_outside") == "1"
                cand["willing_to_relocate"] = data.get("willing_to_relocate") == "1"
                cand["languages"] = [{"language": l, "level": data.get(f"lang_level_{i}", "unverified")}
                                     for i, l in enumerate(data.getlist("lang_name")) if l.strip()]
                wa = {}
                for item in lines("work_authorization"):
                    if ":" in item:
                        c, st = item.split(":", 1)
                        wa[c.strip().upper()] = st.strip()
                cand["work_authorization"] = wa
                put("candidate", cand)
            elif section == "search":
                put("target_countries", {t: lines(t) for t in ("tier1", "tier2", "tier3")})
                put("preferred_roles", lines("preferred_roles"))
                put("excluded_title_terms", lines("excluded_title_terms"))
                put("include_india_remote", data.get("include_india_remote") == "1")
                put("work_arrangements", data.getlist("work_arrangements"))
                put("min_salary", data.get("min_salary") or 0)
                put("currency", data.get("currency"))
                put("sponsorship_policy", data.get("sponsorship_policy"))
                put("min_match_score", data.get("min_match_score"))
            elif section == "weights":
                put("weights", {k: data.get(k) for k in settings_mod.DEFAULT_SETTINGS["weights"]})
            elif section == "limits":
                for k in ("max_applications_per_run", "max_applications_per_day", "max_applications_per_week",
                          "approval_ttl_hours", "verification_max_age_hours", "uncertain_after_hours",
                          "data_retention_days"):
                    put(k, data.get(k))
            elif section == "sources":
                src = {"greenhouse": {"enabled": data.get("greenhouse") == "1", "boards": lines("greenhouse_boards")},
                       "lever": {"enabled": data.get("lever") == "1", "sites": lines("lever_sites")},
                       "ashby": {"enabled": data.get("ashby") == "1", "boards": lines("ashby_boards")},
                       "arbeitnow": {"enabled": data.get("arbeitnow") == "1", "max_pages": data.get("arbeitnow_pages")},
                       "uk_sponsor_register": {"enabled": data.get("uk_sponsor_register") == "1"}}
                put("sources", src)
            elif section == "ai":
                put("ai", {"enabled": data.get("enabled") == "1", "model": data.get("model"),
                           "fallback_model": data.get("fallback_model"),
                           "run_budget_usd": data.get("run_budget_usd"),
                           "monthly_budget_usd": data.get("monthly_budget_usd"),
                           "max_jobs_analyzed_per_run": data.get("max_jobs_analyzed_per_run")})
            elif section == "auto_apply":
                cur = s["auto_apply"]
                enable = data.get("enabled") == "1"
                acked = data.get("acknowledge") == "yes"
                if enable and not acked:
                    return redirect("/settings#auto_apply", "error",
                                    "Tick the authorisation box to enable auto-apply.")
                ack_at = (cur.get("acknowledged_at") or iso()) if acked else None
                put("auto_apply", {
                    "enabled": enable and acked, "acknowledged_at": ack_at, "mode": data.get("mode"),
                    "min_score": data.get("min_score"), "max_per_run": data.get("max_per_run"),
                    "max_per_day": data.get("max_per_day"), "countries": lines("countries"),
                    "sponsorship": data.getlist("sponsorship"), "ats": data.getlist("ats"),
                    "headless": data.get("headless") == "1",
                    "accept_privacy_notices": data.get("accept_privacy_notices") == "1"})
                audit(db, actor=who, entity="settings", entity_id="auto_apply",
                      action="auto_apply_enabled" if enable and acked else "auto_apply_disabled",
                      detail={"user_id": me})
            elif section == "resume_style":
                put("resume_style", {
                    "company_type": data.get("company_type"), "spelling": data.get("spelling"),
                    "one_page": data.get("one_page") == "1", "ai_rewrite": data.get("ai_rewrite") == "1",
                    "tailor_per_job": data.get("tailor_per_job") == "1", "ats_target": data.get("ats_target") or 95})
            elif section == "schedule":
                put("schedule", {"enabled": data.get("enabled") == "1", "time": data.get("time"),
                                 "days": data.getlist("days")})
                put("notifications", {"desktop": data.get("desktop") == "1"})
            elif section == "notifications":
                put("notifications", {"desktop": data.get("desktop") == "1"})
            elif section == "password":
                sess = request.state.session
                try:
                    security.authenticate(db, sess["username"], data.get("current", ""), client_ip(request))
                    if data.get("new") != data.get("new2"):
                        raise security.AuthError("New passwords do not match.")
                    security.set_password(db, sess["username"], data.get("new", ""), who)
                except security.AuthError as e:
                    return redirect("/settings#password", "error", str(e))
                resp = redirect("/login", "ok", "Password changed. Sign in again.")
                resp.delete_cookie(security.SESSION_COOKIE, path="/")
                return resp
            else:
                return redirect("/settings", "error", "Unknown settings section.")
        except settings_mod.SettingsError as e:
            return redirect(f"/settings#{section}", "error", str(e))
        return redirect(f"/settings#{section}", "ok", "Settings saved.")

    @app.get("/settings/export.json")
    def export_json(request: Request):
        db = get_db(request)
        me = uid(request)
        data = {
            "exported_at": iso(),
            "settings": settings_mod.get_all(db, me),
            "applications": db.all("SELECT a.id, a.status, a.external, a.submitted_at, a.reference_number, "
                                   "a.submission_evidence_json, a.notes, a.interview_at, a.follow_up_at, "
                                   "a.rejection_reason, a.offer_details, j.title, j.country, j.apply_url, e.name AS "
                                   "employer FROM applications a JOIN jobs j ON j.id=a.job_id JOIN employers e ON "
                                   "e.id=j.employer_id WHERE a.is_sample=0 AND a.user_id=?", (me,)),
            "resume_versions": resume_store.list_versions(db, user_id=me),
        }
        return Response(dumps(data), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="jobagent-export.json"'})

    # ------------------------------------------------------------------ onboarding (first sign-in questions)
    WELCOME_TITLES = {
        1: ("Let's set up your job search", "Start with your resume (optional). It helps us rank jobs by how well you "
                                            "fit and fill application forms for you."),
        2: ("Which industries are you targeting?", "Choose up to 3. We only fetch and show jobs from these."),
        3: ("Which roles do you want?", "Choose up to 3 roles in your industries, or type your own."),
        4: ("Your experience and the jobs you want", "So we show roles at the right level."),
        5: ("Where do you want to work?", "Countries, way of working and visa needs decide which jobs fit you."),
    }

    def _welcome_ctx(request: Request, step: int, error: str | None = None) -> dict:
        db = get_db(request)
        me = uid(request)
        s = settings_mod.get_all(db, me)
        c = s.get("career") or {}
        resume = resume_store.canonical(db, user_id=me)
        suggest = taxonomy.suggest_from_resume((resume or {}).get("structured")) if resume else \
            {"industries": [], "roles": []}
        cand = s.get("candidate") or {}
        chosen = settings_mod.all_target_countries(s) if (c.get("onboarded_at") or onboarding_mod.missing(db, me)
                                                          .count(5) == 0) else []
        wa = cand.get("work_authorization") or {}
        if not c.get("onboarded_at") and 5 in onboarding_mod.missing(db, me):
            sponsorship = ""
        elif not cand.get("requires_sponsorship_outside"):
            sponsorship = "no"
        elif any(x in wa for x in chosen):
            sponsorship = "some"
        else:
            sponsorship = "yes"
        answered = [n for n in (2, 3, 4, 5) if n not in onboarding_mod.missing(db, me)] + ([1] if resume else [])
        sess = request.state.session
        return {
            "step": step, "steps": onboarding_mod.STEPS, "titles": WELCOME_TITLES, "error": error,
            "editing": bool(sess.get("onboarded_at")), "focus_mode": not sess.get("onboarded_at"),
            "first_name": ((sess.get("full_name") or "").split() or [""])[0],
            "career": {"industries": c.get("industries") or [], "roles": c.get("roles") or [],
                       "custom_roles": c.get("custom_roles") or [], "experience": c.get("experience"),
                       "employment_types": c.get("employment_types") or ["full_time"]},
            "industries": taxonomy.INDUSTRIES, "suggest": suggest, "resume": resume,
            "chosen_industries": [taxonomy.BY_ID[i] for i in c.get("industries") or [] if i in taxonomy.BY_ID],
            "experience": taxonomy.EXPERIENCE, "employment_types": taxonomy.EMPLOYMENT_TYPES,
            "countries_sorted": sorted(settings_mod.COUNTRY_NAMES.items(), key=lambda kv: kv[1]),
            "chosen_countries": chosen, "max_countries": onboarding_mod.MAX_COUNTRIES,
            "arrangements": s.get("work_arrangements") or ["onsite", "hybrid", "remote"],
            "relocate": cand.get("willing_to_relocate", True), "sponsor_choices": onboarding_mod.SPONSOR_CHOICES,
            "sponsorship": sponsorship, "authorised": [x for x in wa],
            "current_location": cand.get("current_location"), "min_salary": s.get("min_salary") or "",
            "currency": s.get("currency"), "currencies": settings_mod.CURRENCIES, "answered": answered,
        }

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome_start(request: Request):
        db = get_db(request)
        if request.state.session.get("onboarded_at"):
            return redirect("/welcome/2")
        todo = onboarding_mod.missing(db, uid(request))
        has_resume = resume_store.canonical(db, user_id=uid(request)) is not None
        return redirect("/welcome/1" if (not has_resume and 2 in todo) else f"/welcome/{todo[0] if todo else 5}")

    @app.get("/welcome/{step}", response_class=HTMLResponse)
    def welcome_step(request: Request, step: int):
        if step not in WELCOME_TITLES:
            return not_found(request)
        db = get_db(request)
        todo = onboarding_mod.missing(db, uid(request))
        # roles need industries first; later steps can be visited in any order
        if step == 3 and 2 in todo:
            return redirect("/welcome/2", "error", "Choose your industries first.")
        return render(request, "welcome.html", _welcome_ctx(request, step))

    def _next_step(db: Database, me: int, after: int) -> str:
        todo = [n for n in onboarding_mod.missing(db, me) if n > after] or onboarding_mod.missing(db, me)
        return f"/welcome/{todo[0]}" if todo else "/welcome/5"

    @app.post("/welcome/1")
    async def welcome_resume(request: Request):
        data = await request.form()
        if not security.csrf_ok(request.state.session, data.get("csrf")):
            raise PermissionError("csrf")
        up = data.get("resume")
        if up is None or not getattr(up, "filename", ""):
            return redirect("/welcome/2")
        content = await up.read(resume_store.MAX_UPLOAD_BYTES + 1)
        db = get_db(request)
        try:
            resume_store.import_bytes(db, content, up.filename, actor=actor(request), user_id=uid(request))
        except ResumeError as e:
            return render(request, "welcome.html", _welcome_ctx(request, 1, e.message), 400)
        return redirect("/welcome/2", "ok", "Resume uploaded. We used it to suggest industries and roles.")

    @app.post("/welcome/{step}")
    async def welcome_save(request: Request, step: int):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        who = actor(request)
        try:
            if step == 2:
                onboarding_mod.save_industries(db, me, who, data.getlist("industry"))
            elif step == 3:
                onboarding_mod.save_roles(db, me, who, data.getlist("role"), data.getlist("custom_role"))
            elif step == 4:
                onboarding_mod.save_experience(db, me, who, data.get("experience", ""),
                                               data.getlist("employment_type"))
            elif step == 5:
                onboarding_mod.save_location(
                    db, me, who, countries=data.getlist("country"), arrangements=data.getlist("arrangement"),
                    sponsorship=data.get("sponsorship", ""), authorised=data.getlist("authorised"),
                    relocate=data.get("relocate") == "1", current_location=data.get("current_location", ""),
                    min_salary=data.get("min_salary", ""), currency=data.get("currency", ""))
            else:
                return not_found(request)
        except onboarding_mod.OnboardingError as e:
            return render(request, "welcome.html", _welcome_ctx(request, step, str(e)), 400)
        if step < 5:
            return redirect(_next_step(db, me, step))
        sess = request.state.session
        try:
            onboarding_mod.finish(db, me, who, account={"full_name": sess.get("full_name"), "email": sess.get("email")})
        except onboarding_mod.OnboardingError as e:
            todo = onboarding_mod.missing(db, me)
            return redirect(f"/welcome/{todo[0]}" if todo else "/welcome/5", "error", str(e))
        n = _personal_search(db, me)
        msg = (f"All set. We found {n} job{'s' if n != 1 else ''} matching your preferences." if n else
               "All set. We're searching for your roles; new jobs appear here after the next agent run "
               "(usually within a day).")
        return redirect("/jobs", "ok", msg)

    def _personal_search(db: Database, me: int) -> int:
        """Fetch jobs for this user's roles right away (a short, time-boxed run), then score them."""
        http = app.state.http_factory()
        try:
            agent_mod.run_agent(db, cfg, trigger="onboarding", http=http, time_budget_s=45, prepare=False,
                                user_ids=[me], triggered_by=me, personal_for=me)
        except Exception as e:  # the user's preferences are saved either way
            _log.warning("personal search failed: %s", e)
        finally:
            http.close()
        s = settings_mod.get_all(db, me)
        sql, params = agent_mod.user_job_scope(s.get("career"))
        return int(db.scalar("SELECT COUNT(DISTINCT j.id) FROM jobs j JOIN job_scores sc ON sc.job_id=j.id "
                             "AND sc.user_id=? "
                             "WHERE j.status='open' AND j.is_sample=0 AND sc.recommendation <> 'skip'" + sql,
                             (me, *params)) or 0)

    # ------------------------------------------------------------------ help & support (users)
    def _notify_admin_ticket(db: Database, t: dict, message: str, kind: str, request: Request) -> None:
        to = cfg.admin_email or db.scalar("SELECT email FROM users WHERE role='admin' AND email IS NOT NULL "
                                          "ORDER BY id LIMIT 1")
        if not to:
            return
        bu = base_url(request)
        link = f"{bu}/admin/support/{t['id']}" if bu else "the Support desk in JobAgent"
        app.state.mailer.send(db, Message(
            to=to, subject=f"[JobAgent support] {t['subject']}", kind=kind, related_user_id=t["user_id"],
            text=(f"{'New request' if kind == 'support_new' else 'New reply'} from {t.get('username') or 'a user'} "
                  f"({helpdesk.CATEGORIES.get(t['category'], t['category'])}):\n\n{message[:1500]}\n\n"
                  f"Answer it here: {link}\n")))

    def _notify_user_reply(db: Database, t: dict, body: str, request: Request) -> None:
        if not t.get("email"):
            return
        bu = base_url(request)
        app.state.mailer.send(db, Message(
            to=t["email"], subject=f"Re: {t['subject']} (JobAgent support)", kind="support_reply",
            related_user_id=t["user_id"],
            text=(f"Hello {t.get('full_name') or t.get('username')},\n\nThe administrator replied to your request:\n\n"
                  f"{body[:3000]}\n\n" + (f"View or reply: {bu}/support/{t['id']}\n" if bu else ""))))

    @app.get("/support", response_class=HTMLResponse)
    def support_page(request: Request, q: str = "", category: str = ""):
        db = get_db(request)
        me = uid(request)
        category = category if category in helpdesk.CATEGORIES else ""
        return render(request, "support.html", {
            "tickets": helpdesk.list_for_user(db, me), "categories": helpdesk.CATEGORIES,
            "statuses": helpdesk.STATUSES, "checks": helpdesk.diagnose(db, me), "q": q[:300], "category": category,
            "suggestions": helpdesk.suggest(db, q, category) if (q.strip() or category) else [],
            "popular": [a for a in helpdesk.KB if a["category"] in ("auto_apply", "companion")][:4],
            "ai": ai_access(db, me)})

    @app.get("/support/suggest")
    def support_suggest(request: Request, q: str = "", category: str = ""):
        db = get_db(request)
        return {"suggestions": helpdesk.suggest(db, q[:500], category if category in helpdesk.CATEGORIES else "")}

    @app.post("/support")
    async def support_create(request: Request):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        try:
            tid = helpdesk.create_ticket(db, user_id=me, category=data.get("category", ""),
                                         subject=data.get("subject", ""), body=data.get("body", ""),
                                         actor=actor(request))
        except helpdesk.SupportError as e:
            return redirect("/support#new", "error", str(e))
        t = helpdesk.get_ticket(db, tid, user_id=me)
        _notify_admin_ticket(db, t, data.get("body", ""), "support_new", request)
        return redirect(f"/support/{tid}", "ok", "Request sent. You'll see the reply here (and by email).")

    @app.post("/support/helpful")
    async def support_helpful(request: Request):
        data = await form(request)
        helpdesk.mark_helpful(get_db(request), data.get("answer_id", ""))
        return redirect("/support", "ok", "Glad that solved it. Thanks for the feedback.")

    @app.get("/support/{tid}", response_class=HTMLResponse)
    def support_ticket(request: Request, tid: int):
        db = get_db(request)
        me = uid(request)
        t = helpdesk.get_ticket(db, tid, user_id=me)
        if not t:
            return not_found(request, "Request not found.")
        helpdesk.mark_seen(db, tid, me)
        text = t["subject"] + " " + (t["messages"][0]["body"] if t["messages"] else "")
        return render(request, "support_ticket.html", {
            "t": t, "categories": helpdesk.CATEGORIES, "statuses": helpdesk.STATUSES, "staff": False,
            "suggestions": helpdesk.suggest(db, text, t["category"], limit=3) if t["status"] == "open" else []})

    @app.post("/support/{tid}/reply")
    async def support_reply(request: Request, tid: int):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        if not helpdesk.get_ticket(db, tid, user_id=me):
            return not_found(request, "Request not found.")
        try:
            helpdesk.reply(db, tid, author_id=me, staff=False, body=data.get("body", ""), actor=actor(request))
        except helpdesk.SupportError as e:
            return redirect(f"/support/{tid}", "error", str(e))
        t = helpdesk.get_ticket(db, tid, user_id=me)
        _notify_admin_ticket(db, t, data.get("body", ""), "support_user_reply", request)
        return redirect(f"/support/{tid}", "ok", "Reply sent.")

    @app.post("/support/{tid}/status")
    async def support_status(request: Request, tid: int):
        data = await form(request)
        db = get_db(request)
        if not helpdesk.get_ticket(db, tid, user_id=uid(request)):
            return not_found(request, "Request not found.")
        try:
            helpdesk.set_status(db, tid, data.get("status", ""), actor=actor(request), user_id=uid(request))
        except helpdesk.SupportError as e:
            return redirect(f"/support/{tid}", "error", str(e))
        return redirect(f"/support/{tid}", "ok", "Marked resolved." if data.get("status") == "resolved"
                        else "Request re-opened.")

    # ------------------------------------------------------------------ application companion (browser extension)
    def _companion_ctx(db: Database, me: int, request: Request, new_token: str | None = None) -> dict:
        return {"tokens": companion.list_tokens(db, me), "new_token": new_token,
                "server": base_url(request) or str(request.base_url).rstrip("/"),
                "profile": companion.profile(db, me), "ai": ai_access(db, me), "token_days": companion.TOKEN_DAYS}

    @app.get("/companion", response_class=HTMLResponse)
    def companion_page(request: Request):
        db = get_db(request)
        return render(request, "companion.html", _companion_ctx(db, uid(request), request))

    @app.post("/companion/tokens", response_class=HTMLResponse)
    async def companion_token_create(request: Request):
        data = await form(request)
        db = get_db(request)
        me = uid(request)
        try:
            token, _row = companion.create_token(db, user_id=me, name=data.get("name", ""), actor=actor(request))
        except companion.CompanionError as e:
            return redirect("/companion", "error", e.message)
        # shown ONCE, in this response only (never stored in plain text, never put in a URL or cookie)
        return render(request, "companion.html", _companion_ctx(db, me, request, token))

    @app.post("/companion/tokens/{token_id}/revoke")
    async def companion_token_revoke(request: Request, token_id: int):
        await form(request)
        try:
            companion.revoke_token(get_db(request), token_id, user_id=uid(request), actor=actor(request))
        except companion.CompanionError as e:
            return redirect("/companion", "error", e.message)
        return redirect("/companion", "ok", "Token revoked. The extension using it is disconnected.")

    @app.get("/companion/extension.zip")
    def companion_download(request: Request):
        data = companion.extension_zip(base_url(request) or str(request.base_url).rstrip("/"))
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="jobagent-companion.zip"'})

    # JSON API for the extension: Bearer-token auth (no cookies, so no CSRF), scoped to the token's user.
    MAX_API_BODY = 512 * 1024

    async def _api_json(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_API_BODY:
            raise companion.CompanionError("Request too large.", 413)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise companion.CompanionError("Invalid JSON.", 400) from None
        if not isinstance(data, dict):
            raise companion.CompanionError("Invalid JSON.", 400)
        return data

    def _api_app_id(v) -> int | None:
        if v in (None, "", 0):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise companion.CompanionError("app_id must be a number.", 400) from None
        if n <= 0 or n > 2**53:
            raise companion.CompanionError("app_id must be a number.", 400)
        return n

    def companion_api(fn):
        @functools.wraps(fn)
        async def wrapper(request: Request):
            u = companion.authenticate(get_db(request), request.headers.get("authorization"))
            if not u:
                return JSONResponse({"error": "Access token invalid or expired. Create a new one on the "
                                              "Application Companion page."}, status_code=401)
            if "/companion" in access.resolve(get_db(request), u["id"])["hidden"]:
                return JSONResponse({"error": "The Application Companion is not enabled for your account. Ask "
                                              "the administrator."}, status_code=403)
            try:
                res = fn(request, u)
                if inspect.isawaitable(res):
                    res = await res
                return res
            except companion.CompanionError as e:
                return JSONResponse({"error": e.message}, status_code=e.status)
        wrapper.__signature__ = inspect.Signature(
            [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)])
        return wrapper

    @app.get("/api/companion/me")
    @companion_api
    def api_c_me(request: Request, u: dict):
        db = get_db(request)
        acc = ai_access(db, u["id"])
        return {"user": {"username": u["username"], "full_name": u["full_name"]},
                "ai": {"allowed": acc["allowed"] and make_provider(cfg).name != "none", "limit": acc["limit"],
                       "spent": round(acc["spent"], 4)},
                "profile": companion.profile(db, u["id"])}

    @app.get("/api/companion/applications")
    @companion_api
    def api_c_apps(request: Request, u: dict):
        db = get_db(request)
        url = request.query_params.get("url", "")[:2000]
        m = companion.match_application(db, u["id"], url) if url else None

        def slim(a):
            return {"id": a["id"], "title": a["title"], "company": a["company"], "status": a["status"],
                    "keys": companion.url_keys(a.get("apply_url"), a.get("url"))}
        return {"match": slim(m) if m else None,
                "applications": [slim(a) for a in companion.active_applications(db, u["id"])]}

    @app.post("/api/companion/plan")
    @companion_api
    async def api_c_plan(request: Request, u: dict):
        data = await _api_json(request)
        db = get_db(request)
        app_id = _api_app_id(data.get("app_id"))
        if app_id is None and data.get("url"):
            m = companion.match_application(db, u["id"], str(data["url"])[:2000])
            app_id = m["id"] if m else None
        res = companion.plan(db, user_id=u["id"], fields=data.get("fields") or [], app_id=app_id)
        host = re.sub(r"^[a-z]+://([^/?#]+).*$", r"\1", str(data.get("url") or ""), flags=re.I)[:120]
        audit(db, actor=f"user:{u['username']}", entity="companion", entity_id=app_id or "", action="companion_fill",
              detail={"user_id": u["id"], "host": host, **res["summary"]})
        return res

    @app.get("/api/companion/resume")
    @companion_api
    def api_c_resume(request: Request, u: dict):
        data, name = companion.resume_file(get_db(request), user_id=u["id"],
                                           app_id=_api_app_id(request.query_params.get("app_id")))
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/companion/learn")
    @companion_api
    async def api_c_learn(request: Request, u: dict):
        data = await _api_json(request)
        return companion.learn(get_db(request), user_id=u["id"], items=data.get("items"),
                               actor=f"user:{u['username']}")

    @app.post("/api/companion/draft")
    @companion_api
    async def api_c_draft(request: Request, u: dict):
        data = await _api_json(request)
        db = get_db(request)
        s = settings_mod.get_all(db, u["id"])
        ai = AIService(db, make_provider(cfg), s["ai"], user_id=u["id"])
        return {"answer": companion.draft_answer(db, ai, user_id=u["id"], question=str(data.get("question") or ""),
                                                 app_id=_api_app_id(data.get("app_id")))}

    @app.post("/api/companion/applied")
    @companion_api
    async def api_c_applied(request: Request, u: dict):
        data = await _api_json(request)
        app_id = _api_app_id(data.get("app_id"))
        if app_id is None:
            raise companion.CompanionError("Pick the JobAgent application this page belongs to.", 400)
        return companion.mark_applied(get_db(request), user_id=u["id"], app_id=app_id,
                                      page_url=str(data.get("url") or ""),
                                      confirmation_text=str(data.get("confirmation_text") or ""),
                                      actor=f"user:{u['username']}")

    # ------------------------------------------------------------------ administration: AI access + support desk
    @app.post("/admin/users/{user_id}/ai-access")
    async def admin_ai_access(request: Request, user_id: int):
        data = await form(request)
        db = get_db(request)
        if not users_mod.get_user(db, user_id):
            return not_found(request, "User not found.")
        grant = data.get("grant") == "1"
        raw = (data.get("monthly_limit_usd") or "").strip() if data.get("ai_mode", "limit") == "limit" else ""
        back = safe_next(data.get("next") or "/admin/users#all-users")
        try:
            limit = float(raw) if raw else None
            if limit is not None and limit != limit:      # NaN
                raise ValueError
        except ValueError:
            return redirect(back, "error", "The monthly AI allowance must be a number (USD).")
        try:
            u = users_mod.set_ai_access(db, user_id, grant, actor(request), actor_id=uid(request),
                                        monthly_limit_usd=limit)
        except users_mod.AccountError as e:
            return redirect(back, "error", str(e))
        users_mod.send_ai_access_notice(db, app.state.mailer, base_url=base_url(request), user=u, granted=grant)
        if grant:
            extra = (f" (allowance ${u['ai_monthly_limit_usd']:.2f}/month)." if u["ai_monthly_limit_usd"] is not None
                     else ".")
            return redirect(back, "ok", f"AI access granted to '{u['username']}'{extra}")
        return redirect(back, "ok", f"AI access removed from '{u['username']}'.")

    @app.post("/admin/users/{user_id}/screens")
    async def admin_user_screens(request: Request, user_id: int):
        data = await form(request)
        db = get_db(request)
        if not users_mod.get_user(db, user_id):
            return not_found(request, "User not found.")
        try:
            access.set_user(db, user_id, data.get("mode", ""), data.getlist("screen"), actor(request))
        except access.AccessError as e:
            return redirect("/admin/users#all-users", "error", str(e))
        return redirect(safe_next(data.get("next") or "/admin/users#all-users"), "ok", "Screens updated.")

    @app.post("/admin/segments/{industry}/screens")
    async def admin_segment_screens(request: Request, industry: str):
        data = await form(request)
        try:
            access.set_segment(get_db(request), industry, data.getlist("screen"), actor(request))
        except (access.AccessError, settings_mod.SettingsError) as e:
            return redirect("/admin/users#audience", "error", str(e))
        return redirect("/admin/users#audience", "ok",
                        f"Screens for {taxonomy.industry_label(industry)} saved. Users with custom screens keep theirs.")

    @app.post("/admin/ai/bulk")
    async def admin_ai_bulk(request: Request):
        data = await form(request)
        db = get_db(request)
        grant = data.get("grant") == "1"
        n = 0
        for u in db.all("SELECT id, ai_access FROM users WHERE role <> 'admin' AND status='approved'"):
            if bool(u["ai_access"]) == grant and grant is False:
                continue
            try:
                users_mod.set_ai_access(db, u["id"], grant, actor(request), actor_id=uid(request),
                                        monthly_limit_usd=None)
                n += 1
            except users_mod.AccountError:
                continue
        return redirect("/admin/users#all-users", "ok",
                        f"AI {'granted (full access, no monthly limit) to' if grant else 'removed from'} {n} account"
                        f"{'s' if n != 1 else ''}.")

    @app.get("/admin/support", response_class=HTMLResponse)
    def admin_support(request: Request, status: str = "active", category: str = "", q: str = ""):
        db = get_db(request)
        return render(request, "admin_support.html", {
            "tickets": helpdesk.list_all(db, status, category, q), "counts": helpdesk.counts(db),
            "status": status, "category": category, "q": q[:80], "categories": helpdesk.CATEGORIES,
            "statuses": helpdesk.STATUSES, "answers": helpdesk.flagged_answers(db)})

    @app.get("/admin/support/{tid}", response_class=HTMLResponse)
    def admin_support_ticket(request: Request, tid: int):
        db = get_db(request)
        t = helpdesk.get_ticket(db, tid, user_id=None)
        if not t:
            return not_found(request, "Request not found.")
        text = t["subject"] + " " + " ".join(m["body"] for m in t["messages"] if not m["is_staff"])
        return render(request, "support_ticket.html", {
            "t": t, "categories": helpdesk.CATEGORIES, "statuses": helpdesk.STATUSES, "staff": True,
            "suggestions": helpdesk.suggest(db, text, t["category"], limit=5),
            "requester_ai": ai_access(db, t["user_id"]), "checks": helpdesk.diagnose(db, t["user_id"])})

    @app.post("/admin/support/{tid}/reply")
    async def admin_support_reply(request: Request, tid: int):
        data = await form(request)
        db = get_db(request)
        t = helpdesk.get_ticket(db, tid, user_id=None)
        if not t:
            return not_found(request, "Request not found.")
        body = data.get("body", "")
        try:
            helpdesk.reply(db, tid, author_id=uid(request), staff=True, body=body, actor=actor(request),
                           resolve=data.get("resolve") == "1")
            msg = "Reply sent."
            if data.get("save_answer") == "1":
                helpdesk.save_answer(db, actor=actor(request), admin_id=uid(request), category=t["category"],
                                     question=(data.get("answer_question") or t["subject"]), answer=body,
                                     keywords=data.get("answer_keywords", ""), source_ticket_id=tid)
                msg += " Saved as a suggested answer: users asking something similar will see it first."
        except helpdesk.SupportError as e:
            return redirect(f"/admin/support/{tid}", "error", str(e))
        _notify_user_reply(db, t, body, request)
        return redirect(f"/admin/support/{tid}", "ok", msg)

    @app.post("/admin/support/{tid}/status")
    async def admin_support_status(request: Request, tid: int):
        data = await form(request)
        db = get_db(request)
        if not helpdesk.get_ticket(db, tid, user_id=None):
            return not_found(request, "Request not found.")
        try:
            helpdesk.set_status(db, tid, data.get("status", ""), actor=actor(request), user_id=None)
        except helpdesk.SupportError as e:
            return redirect(f"/admin/support/{tid}", "error", str(e))
        return redirect(f"/admin/support/{tid}", "ok", "Status updated.")

    @app.post("/admin/support/answers")
    async def admin_answer_save(request: Request):
        data = await form(request)
        db = get_db(request)
        aid = data.get("answer_id") or ""
        try:
            helpdesk.save_answer(db, actor=actor(request), admin_id=uid(request), category=data.get("category", ""),
                                 question=data.get("question", ""), answer=data.get("answer", ""),
                                 keywords=data.get("keywords", ""), answer_id=int(aid) if aid.isdigit() else None)
        except helpdesk.SupportError as e:
            return redirect("/admin/support#answers", "error", str(e))
        return redirect("/admin/support#answers", "ok", "Suggested answer saved.")

    @app.post("/admin/support/answers/{aid}/delete")
    async def admin_answer_delete(request: Request, aid: int):
        await form(request)
        helpdesk.delete_answer(get_db(request), aid, actor=actor(request))
        return redirect("/admin/support#answers", "ok", "Suggested answer deleted.")

    # ------------------------------------------------------------------ administration (admin only; see guard)
    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, segment: str = ""):
        db = get_db(request)
        rows = users_mod.list_users(db)
        aud = audience.overview(db, adzuna_configured=bool(cfg.adzuna_app_id and cfg.adzuna_app_key))
        for u in rows:
            u["profile"] = aud["profiles"].get(u["id"])
            u["access"] = access.resolve(db, u["id"])
        segment = segment if segment in taxonomy.BY_ID or segment == "unset" else ""
        shown = [u for u in rows if not segment or (
            (segment == "unset" and u["profile"] and not u["profile"]["self_reported"]) or
            (u["profile"] and segment in u["profile"]["industries"]))]
        outbox = db.all("SELECT id, kind, to_addr, subject, status, provider, error, created_at, sent_at FROM outbox "
                        "ORDER BY id DESC LIMIT 50")
        return render(request, "admin_users.html", {
            "pending": [u for u in rows if u["status"] == "pending"], "users": shown, "all_count": len(rows),
            "outbox": outbox, "aud": aud, "segment": segment,
            "segment_label": taxonomy.industry_label(segment) if segment in taxonomy.BY_ID else
            ("Preferences not set" if segment == "unset" else ""),
            "chart_industries": charts.hbar(aud["industries_by_users"], "Users by target industry"),
            "chart_levels": charts.hbar(aud["levels"], "Users by experience level"),
            "role_label": taxonomy.role_label, "industry_label": taxonomy.industry_label,
            "screens": access.SCREENS, "screen_labels": access.LABELS, "segment_rules": access.segment_rules(db),
            "matrix": access.matrix(db),
            "adzuna": bool(cfg.adzuna_app_id and cfg.adzuna_app_key),
            "mail_provider": app.state.mailer.provider.name, "admin_email": cfg.admin_email,
            "public_url": base_url(request)})

    @app.post("/admin/users/{user_id}/delete")
    async def admin_delete_user(request: Request, user_id: int):
        data = await form(request)
        db = get_db(request)
        u = users_mod.get_user(db, user_id)
        if not u:
            return not_found(request, "User not found.")
        if (data.get("confirm_username") or "").strip().lower() != u["username"]:
            return redirect("/admin/users", "error", f"Type the username '{u['username']}' to confirm deletion.")
        try:
            users_mod.delete_user(db, user_id, actor(request))
        except users_mod.AccountError as e:
            return redirect("/admin/users", "error", str(e))
        return redirect("/admin/users", "ok", f"Account '{u['username']}' and all of its data were deleted.")

    @app.post("/admin/users/{user_id}/{action}")
    async def admin_user_action(request: Request, user_id: int, action: str):
        await form(request)
        db = get_db(request)
        if action not in users_mod.ALLOWED:
            return not_found(request)
        if not users_mod.get_user(db, user_id):
            return not_found(request, "User not found.")
        try:
            u = users_mod.change_status(db, user_id, action, actor(request), actor_id=uid(request))
        except users_mod.AccountError as e:
            return redirect("/admin/users", "error", str(e))
        if action in ("approve", "reject"):
            users_mod.send_decision(db, app.state.mailer, base_url=base_url(request), user=u, action=action)
        verbs = {"approve": "approved", "reject": "rejected", "suspend": "suspended (signed out everywhere)",
                 "reactivate": "re-activated"}
        return redirect("/admin/users", "ok", f"Account '{u['username']}' {verbs[action]}.")

    @app.post("/admin/test-email")
    async def admin_test_email(request: Request):
        data = await form(request)
        db = get_db(request)
        to = (data.get("to") or cfg.admin_email or request.state.session.get("email") or "").strip().lower()
        from ..mailer import valid_email
        if not valid_email(to):
            return redirect("/admin/users#outbox", "error", "Enter a valid email address for the test message.")
        res = app.state.mailer.send(db, Message(to=to, subject="JobAgent test email",
                                                text="This is a test message from your JobAgent installation.\n",
                                                kind="test"))
        kind = "ok" if res["status"] in ("sent", "logged") else "error"
        msg = {"sent": f"Test email sent via {res['provider']}.",
               "logged": "No email provider is configured; the message was only recorded in the outbox."}.get(
            res["status"], f"Sending failed: {res.get('error') or 'unknown error'}")
        return redirect("/admin/users#outbox", kind, msg)

    # ------------------------------------------------------------------ JSON API (session-protected)
    @app.get("/api/stats")
    def api_stats(request: Request):
        o = queries.overview(get_db(request), user_id=uid(request))
        return {k: o[k] for k in ("jobs_total", "jobs_open", "submitted_total", "awaiting_approval", "interviews",
                                  "offers", "rejections", "problems", "sponsorship_confirmed", "avg_score")}

    @app.get("/api/jobs")
    def api_jobs(request: Request):
        db = get_db(request)
        rows, total = queries.search_jobs(db, queries.JobFilters.from_query(request.query_params),
                                          user_id=uid(request), career=settings_mod.get(db, "career", uid(request)))
        return {"total": total, "jobs": rows}

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    return app
