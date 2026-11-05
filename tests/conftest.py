"""Shared fixtures. All fixture data is SYNTHETIC unless stated (the real resume is read-only)."""
from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from factories import (REAL_RESUME, build_blank_pdf, build_docx, build_encrypted_pdf,  # noqa: E402
                       build_image_only_pdf, build_pdf, ensure_admin, pdf_bytes)

from jobagent.db import Database, open_db  # noqa: E402

PG_URL = os.environ.get("JOBAGENT_TEST_PG_URL",
                        "postgresql://postgres:jobagent_test_pw@127.0.0.1:55432/jobagent_test")
_PG_OK: list = []


def pg_available() -> bool:
    if not _PG_OK:
        try:
            import psycopg
            c = psycopg.connect(PG_URL, autocommit=True, connect_timeout=3)
            c.close()
            _PG_OK.append(True)
        except Exception:
            _PG_OK.append(False)
    return _PG_OK[0]


def pg_reset(url: str = PG_URL) -> None:
    import psycopg
    c = psycopg.connect(url, autocommit=True, connect_timeout=5)
    try:
        # terminate stray connections from earlier tests so DROP SCHEMA cannot block
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() "
                  "AND pid <> pg_backend_pid()")
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        c.close()


# ----------------------------------------------------------------------------- databases

@pytest.fixture
def db(tmp_path):
    """Fresh SQLite DB with the administrator account (id ADMIN) already created."""
    d = open_db("sqlite:///" + str(tmp_path / "test.db"))
    ensure_admin(d)
    yield d
    d.close()


@pytest.fixture
def pg_db():
    if not pg_available():
        pytest.skip("PostgreSQL test database not reachable (set JOBAGENT_TEST_PG_URL)")
    pg_reset()
    d = open_db(PG_URL)
    ensure_admin(d)
    yield d
    d.close()


@pytest.fixture(params=["sqlite", "postgres"])
def anyurl(request, tmp_path):
    """URL of a fresh, EMPTY database of each dialect."""
    if request.param == "sqlite":
        return "sqlite:///" + str(tmp_path / "any.db")
    if not pg_available():
        pytest.skip("PostgreSQL test database not reachable (set JOBAGENT_TEST_PG_URL)")
    pg_reset()
    return PG_URL


@pytest.fixture
def anydb(anyurl):
    """Fresh DB of each dialect with the administrator account (id ADMIN) already created."""
    d = open_db(anyurl)
    ensure_admin(d)
    yield d
    d.close()


# ----------------------------------------------------------------------------- synthetic files

@pytest.fixture(scope="session")
def files(tmp_path_factory):
    """SYNTHETIC resume files of every kind."""
    d = tmp_path_factory.mktemp("synthetic_resumes")
    out = {}
    single = d / "single"
    single.mkdir()
    out["pdf"] = build_pdf(single / "Alex_Example_Resume.pdf")
    out["single_dir"] = single
    out["docx"] = build_docx(d / "Alex_Example_Resume.docx")
    out["corrupted"] = d / "corrupted.pdf"
    out["corrupted"].write_bytes(b"%PDF-1.7\n this is not really a pdf \x00\x01\x02 garbage")
    out["encrypted"] = build_encrypted_pdf(d / "protected.pdf")
    out["image_only"] = build_image_only_pdf(d / "scanned.pdf")
    out["blank"] = build_blank_pdf(d / "blank.pdf")
    out["png"] = d / "resume.png"
    out["png"].write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    multi = d / "multi"
    multi.mkdir()
    build_pdf(multi / "resume_a.pdf")
    build_pdf(multi / "resume_b.pdf")
    out["multi_dir"] = multi
    out["missing_dir"] = d / "does_not_exist" / "resume.pdf"
    out["root"] = d
    return out


@pytest.fixture
def real_resume():
    if not REAL_RESUME.is_file():
        pytest.skip(f"Real resume not present at {REAL_RESUME}")
    return REAL_RESUME


@pytest.fixture
def resume_pdf_bytes():
    return pdf_bytes()


# ----------------------------------------------------------------------------- web client

TEST_USER = "alex"
TEST_PASSWORD = "Correct-Horse-42x"


def _hidden(html: str, name: str) -> str | None:
    m = re.search(r'<input[^>]*name="' + re.escape(name) + r'"[^>]*>', html)
    if not m:
        return None
    v = re.search(r'value="([^"]*)"', m.group(0))
    return v.group(1) if v else None


def extract_csrf(html: str) -> str:
    tok = _hidden(html, "csrf")
    assert tok, "csrf hidden input not found on page"
    return tok


def make_cfg(tmp_path, **kw):
    from jobagent.config import load_config
    cfg = load_config()
    cfg.database_url = "sqlite:///" + str(tmp_path / "web.db")
    cfg.data_dir = tmp_path
    cfg.secret_key = "k" * 40
    cfg.ai_provider = "none"
    cfg.openai_api_key = None
    cfg.anthropic_api_key = None
    cfg.resume_path = None
    cfg.deployment = "local"
    cfg.https = False
    cfg.cron_secret = None
    cfg.setup_token = None
    cfg.host = "127.0.0.1"
    cfg.allow_remote = False
    # email: never a real provider in tests (console provider records to the outbox)
    cfg.admin_email = "owner@example.com"
    cfg.public_url = "http://localhost:8765"
    cfg.resend_api_key = None
    cfg.smtp_host = None
    cfg.mail_from = None
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class WebHelper:
    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    def setup_user(self, username=TEST_USER, password=TEST_PASSWORD):
        r = self.client.get("/setup")
        assert r.status_code == 200, r.text[:300]
        pre = _hidden(r.text, "pre_csrf")
        assert pre and self.client.cookies.get("ja_pre")
        r = self.client.post("/setup", data={"pre_csrf": pre, "username": username, "password": password,
                                             "password2": password}, follow_redirects=False)
        assert r.status_code == 303, r.text[:300]
        return r

    def login(self, username=TEST_USER, password=TEST_PASSWORD, next_url="/", follow=False):
        r = self.client.get("/login")
        pre = _hidden(r.text, "pre_csrf")
        assert pre, "pre_csrf missing on login page"
        return self.client.post("/login", data={"pre_csrf": pre, "username": username, "password": password,
                                                "next": next_url}, follow_redirects=follow)

    def csrf(self, path="/settings") -> str:
        r = self.client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        return extract_csrf(r.text)

    def db(self) -> Database:
        return Database(self.cfg.database_url)

    def logout(self):
        r = self.client.get("/settings")
        tok = _hidden(r.text, "csrf")
        if tok:
            self.client.post("/logout", data={"csrf": tok}, follow_redirects=False)
        self.client.cookies.clear()

    def signup(self, username="bea", password=TEST_PASSWORD, email=None, full_name=None, password2=None):
        r = self.client.get("/signup")
        pre = _hidden(r.text, "pre_csrf")
        assert pre, r.text[:300]
        return self.client.post("/signup", data={
            "pre_csrf": pre, "full_name": full_name or username.title(), "email": email or f"{username}@example.com",
            "username": username, "password": password, "password2": password2 or password},
            follow_redirects=False)


def _templates_ready() -> bool:
    t = Path(__file__).resolve().parent.parent / "jobagent" / "web" / "templates"
    return (t / "base.html").is_file() and (t / "application.html").is_file()


@pytest.fixture
def web(tmp_path):
    """Unauthenticated TestClient + helper on a fresh SQLite DB."""
    if not _templates_ready():
        pytest.skip("Web templates not yet available")
    from fastapi.testclient import TestClient

    from jobagent.web.app import create_app
    cfg = make_cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as c:
        yield WebHelper(c, cfg)


@pytest.fixture
def client(web):
    """Authenticated TestClient (user created via /setup, then logged in)."""
    web.setup_user()
    r = web.login()
    assert r.status_code == 303, r.text[:300]
    return web


def web_factory(tmp_path, mailer=None, **cfg_overrides):
    if not _templates_ready():
        pytest.skip("Web templates not yet available")
    from fastapi.testclient import TestClient

    from jobagent.web.app import create_app
    cfg = make_cfg(tmp_path, **cfg_overrides)
    return WebHelper(TestClient(create_app(cfg, mailer=mailer)), cfg)


@pytest.fixture(autouse=True)
def _no_live_mnc_feeds(request, monkeypatch):
    """Agent-run tests stub `agent.build_sources`; keep the Top-MNC career-site feeds offline too
    (tests in test_j_mnc.py exercise those adapters explicitly with mocked HTTP)."""
    if request.module.__name__.endswith("test_j_mnc"):
        return
    import jobagent.mnc as mnc_mod
    monkeypatch.setattr(mnc_mod, "build_mnc_sources", lambda http, conf, countries: [])


SYNTHETIC_CANDIDATE = {
    "full_name": "Hrithik Rajendra Dwivedi", "email": "candidate.example@example.com", "phone": "+91 90000 00001",
    "linkedin_url": "https://www.linkedin.com/in/hrithik-dwivedi/", "current_location": "Mumbai, India",
    "nationality": "Indian", "citizenship_countries": ["IN"], "work_authorization": {"IN": "citizen"},
    "requires_sponsorship_outside": True, "languages": [{"language": "English", "level": "unverified"}],
    "willing_to_relocate": True,
}


@pytest.fixture(autouse=True)
def _synthetic_candidate(monkeypatch):
    """Personal candidate defaults live outside the code (JOBAGENT_CANDIDATE_JSON); tests use a SYNTHETIC
    candidate (real name/LinkedIn handle only, so resume-consistency checks still apply; fake email/phone)."""
    import json
    monkeypatch.setenv("JOBAGENT_CANDIDATE_JSON", json.dumps(SYNTHETIC_CANDIDATE))
