"""Auto-apply worker tests. Fully OFFLINE: SYNTHETIC ATS-like forms are served by a local ThreadingHTTPServer and
the browser maps the public ATS hosts to it (worker `allow_hosts` test hook); every other host is blocked."""
from __future__ import annotations

import email.parser
import email.policy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import wf
from factories import ADMIN, agent_config
from jobagent import applications as apps
from jobagent import settings as settings_mod
from jobagent.autoapply import matcher as M
from jobagent.autoapply.captcha import detect_in_html
from jobagent.db import dumps, iso, loads, named_lock

FIXTURES = Path(__file__).parent / "fixtures" / "ats"
GH_HOST, LEVER_HOST, ASHBY_HOST = "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com"
LEVER_ID = "6ed76ce8-4156-4b60-b120-403538bd66cd"
ASHBY_ID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"

CANDIDATE = {   # SYNTHETIC candidate (never the real user's data)
    "full_name": "Alex Quinn Example", "email": "alex.example@example.com", "phone": "+91 90000 12345",
    "linkedin_url": "https://www.linkedin.com/in/alex-example", "current_location": "Mumbai, India",
    "nationality": "Indian", "citizenship_countries": ["IN"], "work_authorization": {"IN": "citizen"},
    "requires_sponsorship_outside": True, "languages": [{"language": "English", "level": "unverified"}],
    "notice_period": None, "salary_expectation": None, "willing_to_relocate": True,
}
RESUME = {"name": "Alex Example", "contact": {"email": "alex.example@example.com", "phone": "+91 90000 12345",
                                              "location": "Mumbai, India"},
          "summary": "Software engineer building C# and .NET services.",
          "skills": {"Languages": ["C#", "Python", "SQL"]},
          "experience": [{"title": "Software Engineer", "company": "Acme Synthetic Ltd", "location": "Mumbai",
                          "start": "Jan 2023", "end": "Present", "bullets": ["Built ASP.NET Core APIs."]}],
          "education": [], "projects": []}
COVER = "Dear Hiring Team, I would love to build reliable .NET services with you. (synthetic)"

CAPTCHAS = {
    "recaptcha_script": ('<script src="https://www.google.com/recaptcha/api.js?render=6Lc_synthetic"></script>', ""),
    "recaptcha_iframe": ("", '<div class="g-recaptcha" data-sitekey="6Lc_synthetic"><iframe '
                             'src="https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"></iframe></div>'),
    "hcaptcha": ('<script src="https://js.hcaptcha.com/1/api.js" async defer></script>',
                 '<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>'),
    "turnstile": ('<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>',
                  '<div class="cf-turnstile" data-sitekey="1x00000000000000000000AA"></div>'),
}

CONFIRM_GH = ('<html><body><div id="application_confirmation"><h1>Thank you for applying.</h1><p>Your application '
              'has been received and our team will review it.</p></div></body></html>')
CONFIRM_LEVER = ("<html><body><div class='application-confirmation'><h3>Application submitted!</h3>"
                 "<p>Thanks for applying to Acme.</p></div></body></html>")


# ----------------------------------------------------------------------------- fixture server

class FixtureServer:
    def __init__(self):
        self.pages: dict[str, list] = {}     # path -> [(status, body, headers)], last one repeats
        self.post_routes: dict[str, tuple] = {}
        self.posts: list[dict] = []
        self.gets: list[str] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                hdrs = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                srv.gets.append(path)
                seq = srv.pages.get(path)
                if not seq:
                    return self._send(404, "not found")
                status, body, headers = seq.pop(0) if len(seq) > 1 else seq[0]
                self._send(status, body, headers)

            def do_POST(self):
                path = urlsplit(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                srv.posts.append({"path": path, **_parse_body(self.headers.get("Content-Type", ""), raw)})
                status, body, headers = srv.post_routes.get(path, (404, "not found", {}))
                self._send(status, body, headers)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def page(self, path, *responses):
        self.pages[path] = [r if isinstance(r, tuple) else (200, r, {}) for r in responses]

    def on_post(self, path, status, body="", headers=None):
        self.post_routes[path] = (status, body, headers or {})

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _parse_body(ctype: str, raw: bytes) -> dict:
    fields, files = {}, {}
    if ctype.startswith("multipart/"):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True) or b""
            if part.get_filename() is not None:
                if part.get_filename():
                    files[name] = {"filename": part.get_filename(), "size": len(data), "head": data[:8],
                                   "text": data[:2000].decode("utf-8", "ignore")}
            else:
                fields.setdefault(name, []).append(data.decode("utf-8", "ignore"))
    else:
        for k, v in parse_qs(raw.decode("utf-8", "ignore"), keep_blank_values=True).items():
            fields[k] = v
    return {"fields": {k: (v[0] if len(v) == 1 else v) for k, v in fields.items()}, "files": files}


# Chromium does not expose file bytes of intercepted multipart bodies, so the fixture page itself reports what was
# attached (name|size|type) in a hidden field next to each file input.
FILE_META_JS = """<script>
document.querySelectorAll('input[type=file]').forEach(function (f) {
  f.addEventListener('change', function () {
    var h = document.createElement('input'); h.type = 'hidden'; h.name = (f.name || f.id) + '__meta';
    var x = f.files[0]; h.value = x ? (x.name + '|' + x.size + '|' + x.type) : '';
    (f.form || f.parentElement).appendChild(h);
  });
});
</script>"""


def render(name: str, *, action: str, captcha: str | None = None, extra: str = "") -> str:
    html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
    head, body = CAPTCHAS[captcha] if captcha else ("", "")
    return (html.replace("{{ACTION}}", action).replace("{{CAPTCHA_HEAD}}", head).replace("{{CAPTCHA_BODY}}", body)
            .replace("{{EXTRA}}", extra).replace("</body>", FILE_META_JS + "</body>"))


def file_meta(fields: dict, name: str) -> tuple[str, int, str]:
    fname, size, ctype = fields[name + "__meta"].split("|")
    return fname, int(size), ctype


# ----------------------------------------------------------------------------- browser / server fixtures

@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (pip install -r requirements-worker.txt)")
    try:
        pw = sync_playwright().start()
    except Exception as e:
        pytest.skip(f"playwright unavailable: {e}")
    try:
        # Belt and braces: no hostname except the local fixture server can resolve (fully offline).
        b = pw.chromium.launch(headless=True, args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
    except Exception as e:
        pw.stop()
        pytest.skip(f"Chromium not installed (python -m playwright install chromium): {str(e)[:80]}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def server():
    s = FixtureServer()
    yield s
    s.close()


# ----------------------------------------------------------------------------- DB helpers

def enable(db, **kw):
    pol = dict(settings_mod.DEFAULT_SETTINGS["auto_apply"], enabled=True, acknowledged_at=iso(), max_per_run=5,
               max_per_day=10)
    pol.update(kw)
    settings_mod.set_value(db, "auto_apply", pol, "user:alex", user_id=ADMIN)


def setup_candidate(db):
    settings_mod.set_value(db, "candidate", CANDIDATE, "user:alex", user_id=ADMIN)


def add_resume(db) -> int:
    rid = db.scalar("SELECT id FROM resume_versions WHERE label='synthetic-aa' AND user_id=?", (ADMIN,))
    if rid:
        return rid
    return db.insert("INSERT INTO resume_versions(label, kind, is_canonical, structured_json, method, status, user_id, "
                     "created_at) VALUES ('synthetic-aa','original',1,?,'parse','approved',?,?)", (dumps(RESUME), ADMIN, iso()))


def make_app(db, n=1, *, ats="greenhouse", score=88.0, sponsorship="confirmed", location=None, title=None,
             company="Acme", prepare=True) -> dict:
    setup_candidate(db)
    if ats == "greenhouse":
        url, loc = f"https://{GH_HOST}/acme/jobs/{n}", location or "Dublin, Ireland"
    elif ats == "lever":
        url, loc = f"https://{LEVER_HOST}/acme/{LEVER_ID[:-2]}{n:02d}/apply", location or "London, United Kingdom"
    elif ats == "ashby":
        url, loc = f"https://{ASHBY_HOST}/acme/{ASHBY_ID[:-2]}{n:02d}/application", location or "Toronto, Canada"
    else:
        url, loc = ats, location or "Dublin, Ireland"
    jid = wf.add_job(db, n, apply_url=url, url=url, location_raw=loc, title=title or f"Software Engineer {n}",
                     company=company)
    db.run("INSERT INTO job_scores(job_id, user_id, score, recommendation, breakdown_json, details_json, weights_json, "
           "created_at) VALUES (?,?,?,?,?,?,?,?)",
           (jid, ADMIN, score, "apply", "{}", dumps({"sponsorship": {"classification": sponsorship,
                                                              "needs_sponsorship": True}, "disqualifiers": []}),
            "{}", iso()))
    if not prepare:
        return {"job_id": jid, "url": url}
    a = apps.prepare(db, jid, "agent", add_resume(db), COVER, "template", user_id=ADMIN)
    return dict(a, url=url)


def run(db, tmp_path, browser, server, **kw):
    from jobagent.autoapply import run_autoapply
    cfg = agent_config(db.url, data_dir=tmp_path)
    opts = dict(browser=browser, allow_hosts={GH_HOST: server.origin, LEVER_HOST: server.origin,
                                              ASHBY_HOST: server.origin, "boards.greenhouse.io": server.origin},
                pause=(0, 0), field_delay=(0, 0), confirm_timeout=6, assist_wait=0, headless=True)
    opts.update(kw)
    return run_autoapply(db, cfg, **opts)


def status(db, app_id):
    return db.scalar("SELECT status FROM applications WHERE id=?", (app_id,))


def path_of(url):
    return urlsplit(url).path


def gh_serve(server, app, *, captcha=None, extra="", post=None):
    p = path_of(app["url"])
    server.page(p, render("greenhouse", action=p + "/applications", captcha=captcha, extra=extra))
    server.page(p + "/confirmation", CONFIRM_GH)
    if post is None:
        server.on_post(p + "/applications", 303, "", {"Location": p + "/confirmation"})
    else:
        server.on_post(p + "/applications", *post)
    return p


def handback_events(db, app_id):
    return [loads(r["detail_json"], {}) for r in db.all(
        "SELECT detail_json FROM audit_events WHERE entity='application' AND entity_id=? AND "
        "action='autoapply_handback' ORDER BY id", (str(app_id),))]


# ============================================================================= matcher unit tests (no browser)

def _m(country, **cand):
    return M.Matcher(dict(CANDIDATE, **cand), {"country": country}, [], RESUME, {"accept_privacy_notices": False},
                     COVER)


def test_matcher_sponsorship_and_work_authorisation_by_country():
    auth = M.Field("k", "Are you legally authorized to work in the country this job is located?*", "radio", True,
                   ["Yes", "No"])
    spons = M.Field("k", "Will you now or in the future require visa sponsorship?", "select", True,
                    ["Select...", "Yes", "No"])
    ie, india = _m("IE"), _m("IN")
    assert ie.decide(auth).value == "No" and ie.decide(spons).value == "Yes"
    assert india.decide(auth).value == "Yes" and india.decide(spons).value == "No"
    # A country named in the label wins over the job's country.
    us_q = M.Field("k", "Are you authorized to work in India?", "radio", True, ["Yes", "No"])
    assert ie.decide(us_q).value == "Yes"
    uk_q = M.Field("k", "Do you have the right to work in the UK?", "buttons", True, ["Yes", "No"])
    assert india.decide(uk_q).value == "No"
    # Unclear option wording -> not automatable (never guessed)
    vague = M.Field("k", "Will you require sponsorship?", "radio", True,
                    ["I will require sponsorship", "I will not require sponsorship"])
    assert india.decide(vague).action == "unknown"


def test_matcher_never_answers_salary_or_notice_when_unset():
    m = _m("IE")
    for label in ("What are your salary expectations?", "Notice period", "Earliest start date"):
        assert m.decide(M.Field("k", label, "text", True)).action == "unknown"
        assert m.decide(M.Field("k", label, "text", False)).action == "skip"
    m2 = _m("IE", notice_period="1 month", salary_expectation="EUR 60,000")
    assert m2.decide(M.Field("k", "Notice period", "text", True)).value == "1 month"
    assert m2.decide(M.Field("k", "Salary expectation", "text", True)).value == "EUR 60,000"


def test_matcher_phone_formatting():
    assert M.format_phone("+91 90000 12345") == "+91 9000012345"
    assert M.format_phone("(+91) 90000-12345") == "+91 9000012345"
    assert M.format_phone("+91 90000 12345", national=True) == "9000012345"
    assert M.format_phone("0091 9000012345") == "+91 9000012345"
    assert M.format_phone("") is None
    m = _m("IE")
    assert m.decide(M.Field("k", "Phone*", "tel", True)).value == "+91 9000012345"
    assert m.decide(M.Field("k", "Phone number (without country code)", "tel", True)).value == "9000012345"
    assert m.decide(M.Field("k", "Country code", "text", True)).value == "+91"


def test_matcher_names_eeo_consent_and_unknowns():
    m = _m("IE")
    assert m.decide(M.Field("k", "First Name*", "text", True)).value == "Alex"
    assert m.decide(M.Field("k", "Last Name*", "text", True)).value == "Example"
    assert m.decide(M.Field("k", "Legal Name (if different than above)", "text", False)).action == "skip"
    assert m.decide(M.Field("k", "Current company", "text", False)).value == "Acme Synthetic Ltd"
    g = m.decide(M.Field("k", "Gender", "select", False, ["Male", "Female", "Decline To Self Identify"]))
    assert (g.action, g.value, g.source) == ("choose", "Decline To Self Identify", "default_decline")
    assert m.decide(M.Field("k", "Are you Hispanic/Latino?", "radio", True, ["Yes", "No"])).action == "unknown"
    assert m.decide(M.Field("k", "Race", "select", False, ["Asian", "White"])).action == "skip"
    consent = M.Field("k", "I agree to the Acme Candidate Privacy Notice*", "checkbox", True, ["I agree"])
    assert m.decide(consent).action == "needs_consent"
    ok = M.Matcher(CANDIDATE, {"country": "IE"}, [], RESUME, {"accept_privacy_notices": True}, COVER)
    assert ok.decide(consent).action == "check"
    assert m.decide(M.Field("k", "I'd like to receive marketing emails", "checkbox", False)).action == "skip"
    assert m.decide(M.Field("k", "Have you previously worked at Acme?*", "radio", True, ["Yes", "No"])).blocking
    assert m.decide(M.Field("k", "GitHub URL", "text", True)).blocking          # not in Settings -> never invented
    assert m.decide(M.Field("k", "", "text", True)).blocking
    hh = m.decide(M.Field("k", "How did you hear about this job?", "select", True,
                          ["LinkedIn", "Company Website", "Referral", "Other"]))
    assert hh.value == "Company Website"


def test_matcher_user_answers_take_precedence():
    m = M.Matcher(CANDIDATE, {"country": "IE"}, [{"question": "Are you willing to relocate?", "answer": "No",
                                                  "source": "user"}], RESUME, {}, COVER)
    assert m.decide(M.Field("k", "Are you willing to relocate?*", "radio", True, ["Yes", "No"])).value == "No"


def test_choose_option_ambiguity_and_countries():
    assert M.choose_option(["Ireland +353", "India +91", "British Indian Ocean Territory +246"], "India") == "India +91"
    assert M.choose_option(["Yes", "Yes, definitely"], "Yes") == "Yes"
    assert M.choose_option(["Yes - Intern", "Yes - Full Time"], "Yes") is None
    assert M.countries_in(M.norm("Please let us know if you need sponsorship")) == set()
    assert M.countries_in(M.norm("Are you authorized to work in the US?")) == {"US"}


def test_resume_pdf_generated_from_resume_version(db):
    from jobagent.autoapply.worker import _resume_pdf
    a = make_app(db)
    data = _resume_pdf(db, apps._get_app(db, a["id"], ADMIN))
    assert data.startswith(b"%PDF") and len(data) > 1000


def test_captcha_html_detection():
    assert detect_in_html('<script src="https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js">') == ["recaptcha"]
    assert detect_in_html('<div class="h-captcha"></div>') == ["hcaptcha"]
    assert detect_in_html('<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">') == ["turnstile"]
    assert detect_in_html("<title>Just a moment...</title>") == ["challenge"]
    assert detect_in_html("<form><input name=email></form>") == []


# ============================================================================= worker (browser) tests

def test_disabled_does_nothing(db, tmp_path, browser, server):
    a = make_app(db)
    gh_serve(server, a)
    res = run(db, tmp_path, browser, server)
    assert res["status"] == "disabled"
    assert status(db, a["id"]) == "awaiting_approval" and server.gets == [] and server.posts == []


def test_greenhouse_happy_path(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    p = gh_serve(server, a)
    res = run(db, tmp_path, browser, server)
    assert res["status"] == "succeeded", res
    assert res["stats"]["submitted_confirmed"] == 1, res
    assert status(db, a["id"]) == "submitted"
    assert len(server.posts) == 1
    post = server.posts[0]
    f = post["fields"]
    assert post["path"] == p + "/applications"
    assert f["first_name"] == "Alex" and f["last_name"] == "Example"
    assert f["email"] == "alex.example@example.com"
    assert f["phone"] == "+91 9000012345" and f["phone_country"] == "India +91"
    assert f["location"] == "Mumbai, Maharashtra, India"
    assert f["question_1"] == "https://www.linkedin.com/in/alex-example"
    assert f["question_2"] == ""                                   # website not in Settings -> left blank
    assert f["question_3"] == "No" and f["question_4"] == "Yes"   # IE: not authorised, needs sponsorship
    assert f["question_5"] == "Company Website"
    assert f["cover_letter_text"] == COVER                         # pasted, not uploaded
    assert "cover_letter" not in post["files"]
    assert f["website_hp"] == ""                                   # invisible honeypot untouched
    assert post["files"]["resume"]["filename"] == "Alex_Example_Resume.pdf"
    fname, size, ctype = file_meta(f, "resume")
    assert fname == "Alex_Example_Resume.pdf" and size > 1000 and ctype == "application/pdf"
    # voluntary EEO: decline options only
    assert f["gender"] == "Decline To Self Identify" and f["hispanic_ethnicity"] == "Decline To Self Identify"
    assert f["veteran_status"] == "I don't wish to answer" and f["disability_status"] == "I do not want to answer"
    app = db.one("SELECT * FROM applications WHERE id=?", (a["id"],))
    ev = loads(app["submission_evidence_json"], {})
    assert "Thank you for applying" in ev["confirmation_text"] and ev["recorded_by"] == apps.AUTO_ACTOR
    fobj = db.one("SELECT kind, content_type, data FROM files WHERE id=?", (ev["file_id"],))
    assert fobj["kind"] == "submission_evidence" and bytes(fobj["data"]).startswith(b"\x89PNG")
    assert db.scalar("SELECT COUNT(*) FROM uploads WHERE application_id=?", (a["id"],)) == 1
    assert app["auto_apply"] == 1 and app["attempts"] == 1
    assert db.one("SELECT approved_by, consumed_at FROM approvals WHERE application_id=?", (a["id"],))["consumed_at"]
    run_row = db.one("SELECT kind, status, stats_json FROM runs WHERE id=?", (res["run_id"],))
    assert run_row["kind"] == "autoapply" and loads(run_row["stats_json"])["submitted_confirmed"] == 1
    # duplicate prevention: a second run does nothing for the submitted application
    res2 = run(db, tmp_path, browser, server)
    assert res2["stats"]["considered"] == 0 and len(server.posts) == 1


def test_lever_happy_path(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db, ats="lever")
    p = path_of(a["url"])
    thanks = p.rsplit("/", 1)[0] + "/thanks"
    server.page(p, render("lever", action=p))
    server.page(thanks, CONFIRM_LEVER)
    server.on_post(p, 302, "", {"Location": thanks})
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1, res
    f = server.posts[0]["fields"]
    assert f["name"] == "Alex Quinn Example" and f["email"] == "alex.example@example.com"
    assert f["phone"] == "+91 9000012345" and f["location"] == "Mumbai, India"
    assert f["org"] == "Acme Synthetic Ltd" and f["urls[LinkedIn]"] == "https://www.linkedin.com/in/alex-example"
    assert f["urls[GitHub]"] == ""
    assert f["cards[c1][field0]"] == "No" and f["cards[c1][field1]"] == "Yes"     # GB job
    assert f["cards[c2][field0]"] == "Company website"
    assert f["comments"] == COVER
    assert "pronouns" not in f                                                  # optional, never guessed
    assert f["surveysResponses[s1][responses][field0]"] == "Prefer not to disclose"
    assert f["surveysResponses[s1][responses][field1]"] == "Prefer not to disclose"
    assert file_meta(f, "resume")[0] == "Alex_Example_Resume.pdf" and file_meta(f, "resume")[1] > 1000
    assert status(db, a["id"]) == "submitted"
    ev = loads(db.scalar("SELECT submission_evidence_json FROM applications WHERE id=?", (a["id"],)))
    assert "Application submitted" in ev["confirmation_text"]


def test_ashby_happy_path(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db, ats="ashby")
    p = path_of(a["url"])
    server.page(p, render("ashby", action=p + "/submit"))
    server.on_post(p + "/submit", 200, json.dumps({"success": True}), {"Content-Type": "application/json"})
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1, res
    post = server.posts[0]
    f = post["fields"]
    assert f["_systemfield_name"] == "Alex Quinn Example" and f["_systemfield_email"] == "alex.example@example.com"
    assert f["a1b2c3d4-0000-4000-8000-000000000001"] == "+91 9000012345"
    assert f["a1b2c3d4-0000-4000-8000-000000000002"] == COVER
    assert f["a1b2c3d4-0000-4000-8000-000000000004"] == "India"
    assert f["a1b2c3d4-0000-4000-8000-000000000005"] == ""            # notice period unset -> blank
    assert f["a1b2c3d4-0000-4000-8000-000000000006"] == "No"          # CA authorisation
    assert f["a1b2c3d4-0000-4000-8000-000000000007"] == "Yes"         # sponsorship
    assert file_meta(f, "_systemfield_resume")[:1] == ("Alex_Example_Resume.pdf",)
    assert status(db, a["id"]) == "submitted"
    ev = loads(db.scalar("SELECT submission_evidence_json FROM applications WHERE id=?", (a["id"],)))
    assert "Thank you for applying" in ev["confirmation_text"]


UNKNOWN_Q = """<div class="field-wrapper"><div class="select">
  <label id="question_9-label" for="question_9" class="label">Have you previously worked at Acme?<span class="required">*</span></label>
  <div class="select__container"><input id="question_9" role="combobox" aria-required="true" aria-expanded="false"
    class="select__input" autocomplete="off" data-options="Yes|No"><div class="select__single-value"></div>
    <input type="hidden" name="question_9" value=""></div></div></div>"""
CONSENT_Q = """<div class="field-wrapper"><fieldset><legend>Data privacy</legend>
  <label><input type="checkbox" id="consent" name="consent" required> I have read and agree to the Acme Candidate
  Privacy Notice<span class="required">*</span></label></fieldset></div>"""


def test_unknown_required_question_is_handed_back(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    gh_serve(server, a, extra=UNKNOWN_Q)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["handed_back"] == 1 and res["stats"]["policy_approved"] == 1, res
    assert status(db, a["id"]) == "awaiting_approval"
    assert apps.active_approval(db, a["id"], user_id=ADMIN) is None
    assert server.posts == []
    ev = handback_events(db, a["id"])
    assert ev and ev[-1]["code"] == "unknown_questions"
    assert ev[-1]["unknown_questions"] == ["Have you previously worked at Acme?"]
    assert "alex" not in json.dumps(ev).lower()                       # no personal data in the audit
    # not re-tried on the next run until the user changes the application
    res2 = run(db, tmp_path, browser, server)
    assert res2["stats"]["considered"] == 0
    # the normal assisted flow still works for the user
    assert apps.approval_gate_checks(db, apps._get_app(db, a["id"], ADMIN), settings_mod.get_all(db, ADMIN)) == []


@pytest.mark.parametrize("kind", ["recaptcha_script", "recaptcha_iframe", "hcaptcha", "turnstile"])
def test_captcha_headless_is_handed_back(db, tmp_path, browser, server, kind):
    enable(db)
    a = make_app(db)
    gh_serve(server, a, captcha=kind)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["handed_back"] == 1, res
    assert res["results"][0]["code"] == "captcha"
    assert status(db, a["id"]) == "awaiting_approval" and server.posts == []


def test_captcha_headed_fills_and_leaves_for_user(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db, ats="lever")
    p = path_of(a["url"])
    server.page(p, render("lever", action=p, captcha="hcaptcha"))
    server.on_post(p, 302, "", {"Location": p + "/thanks"})
    res = run(db, tmp_path, browser, server, headless=False)       # logical "headed" (browser itself is headless)
    assert res["stats"]["fill_only_left_open"] == 1, res
    assert status(db, a["id"]) == "application_started"
    assert server.posts == []                                        # never clicked submit
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE entity_id=? AND action='autoapply_filled_for_review'",
                     (str(a["id"]),)) == 1


def test_fill_only_never_clicks_submit(db, tmp_path, browser, server):
    enable(db, mode="fill_only")
    a = make_app(db)
    gh_serve(server, a)
    res = run(db, tmp_path, browser, server, headless=False)
    assert res["stats"]["fill_only_left_open"] == 1, res
    assert server.posts == [] and status(db, a["id"]) == "application_started"
    # fill_only in a headless environment cannot hand over to the user -> handed back instead
    b = make_app(db, 2)
    gh_serve(server, b)
    res = run(db, tmp_path, browser, server, headless=True)
    assert res["stats"]["handed_back"] == 1 and status(db, b["id"]) == "awaiting_approval"


def test_no_confirmation_is_uncertain_and_not_retried(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    gh_serve(server, a, post=(200, "<html><body><p>Processing your request</p></body></html>", {}))
    res = run(db, tmp_path, browser, server, confirm_timeout=2)
    assert res["stats"]["uncertain"] == 1, res
    assert status(db, a["id"]) == "submission_uncertain" and len(server.posts) == 1
    res2 = run(db, tmp_path, browser, server)
    assert res2["stats"]["considered"] == 0 and len(server.posts) == 1


def test_server_error_before_submit_is_failed_before_submit(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    p = gh_serve(server, a)
    form = server.pages[p][0]
    server.page(p, form, (500, "Internal Server Error", {}))        # preflight OK, fresh load after start fails
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["failed"] == 1, res
    assert status(db, a["id"]) == "submission_failed" and server.posts == []
    last = db.one("SELECT detail_json FROM audit_events WHERE entity_id=? AND to_status='submission_failed'",
                  (str(a["id"]),))
    assert loads(last["detail_json"])["stage"] == "before_submit"


def test_dry_run_changes_nothing(db, tmp_path, browser, server):
    a = make_app(db)                                   # policy NOT enabled: a dry run may still preview
    gh_serve(server, a)
    before = db.one("SELECT status, row_version, attempts FROM applications WHERE id=?", (a["id"],))
    res = run(db, tmp_path, browser, server, dry_run=True)
    assert res["stats"]["dry_run"] == 1, res
    r = res["results"][0]
    assert r["outcome"] == "dry_run" and "Email" in r["filled"] and Path(r["screenshot"]).is_file()
    assert db.one("SELECT status, row_version, attempts FROM applications WHERE id=?", (a["id"],)) == before
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE application_id=?", (a["id"],)) == 0
    assert server.posts == []


def test_not_eligible_jobs_untouched(db, tmp_path, browser, server):
    enable(db, max_per_day=10)
    low = make_app(db, 1, score=60)
    nosp = make_app(db, 2, sponsorship="not_supported")
    other = make_app(db, 3, ats="https://careers.example.com/jobs/3")
    for x in (low, nosp):
        gh_serve(server, x)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["not_eligible"] == 3 and res["stats"]["policy_approved"] == 0, res
    for x in (low, nosp, other):
        assert status(db, x["id"]) == "awaiting_approval"
    assert server.gets == [] and server.posts == []


def test_daily_cap_respected(db, tmp_path, browser, server):
    enable(db, max_per_day=1)
    a, b = make_app(db, 1), make_app(db, 2, score=80)
    gh_serve(server, a)
    gh_serve(server, b)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1 and res["stats"]["not_eligible"] == 1, res
    assert status(db, a["id"]) == "submitted" and status(db, b["id"]) == "awaiting_approval"


def test_max_per_run_and_limit(db, tmp_path, browser, server):
    enable(db, max_per_run=1)
    a, b = make_app(db, 1), make_app(db, 2, score=80)
    gh_serve(server, a)
    gh_serve(server, b)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1 and status(db, b["id"]) == "awaiting_approval"
    enable(db, max_per_run=5)
    res = run(db, tmp_path, browser, server, limit=0)
    assert res["stats"]["considered"] == 0 and status(db, b["id"]) == "awaiting_approval"


def test_equivalent_vacancy_not_applied_twice(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db, 1, title="Backend Engineer")
    # a repost of the same vacancy (same employer/title/country) under another URL
    db.run("INSERT INTO jobs(job_key, employer_id, title, normalized_title, requisition_id, location_raw, country, "
           "remote_type, remote_scope, description_text, first_discovered_at, last_seen_at, last_verified_at, status, "
           "apply_url, canonical_url, is_sample) SELECT job_key || '-repost', employer_id, title, normalized_title, "
           "requisition_id, location_raw, country, remote_type, remote_scope, description_text, first_discovered_at, "
           "last_seen_at, last_verified_at, status, ?, ?, is_sample FROM jobs WHERE id=?",
           (f"https://{GH_HOST}/acme/jobs/77", f"https://{GH_HOST}/acme/jobs/77", a["job_id"]))
    jid2 = db.scalar("SELECT id FROM jobs WHERE apply_url=?", (f"https://{GH_HOST}/acme/jobs/77",))
    db.run("INSERT INTO job_scores(job_id, user_id, score, recommendation, breakdown_json, details_json, weights_json, "
           "created_at) SELECT ?, user_id, 80, recommendation, breakdown_json, details_json, weights_json, created_at FROM "
           "job_scores WHERE job_id=?", (jid2, a["job_id"]))
    b = apps.prepare(db, jid2, "agent", add_resume(db), COVER, "template", user_id=ADMIN)
    gh_serve(server, a)
    gh_serve(server, dict(b, url=f"https://{GH_HOST}/acme/jobs/77"))
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1, res
    assert status(db, b["id"]) == "awaiting_approval" and len(server.posts) == 1


def test_destination_guard_refuses_other_host(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    p = path_of(a["url"])
    server.page(p, (302, "", {"Location": "https://evil.example.net/apply"}))
    res = run(db, tmp_path, browser, server)
    assert res["results"][0]["code"] == "destination_mismatch", res
    assert status(db, a["id"]) == "awaiting_approval" and server.posts == []
    # JavaScript redirect after load is blocked too
    b = make_app(db, 2)
    q = path_of(b["url"])
    server.page(q, "<html><body><form id='application-form'><input type=email></form><script>"
                   "location.href='https://evil.example.net/apply'</script></body></html>")
    res = run(db, tmp_path, browser, server)
    assert res["results"][0]["code"] == "destination_mismatch", res
    assert status(db, b["id"]) == "awaiting_approval"


def test_concurrent_workers_lock(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    gh_serve(server, a)
    with named_lock(db, "autoapply"):
        res = run(db, tmp_path, browser, server)
    assert res["status"] == "locked" and server.gets == []
    assert status(db, a["id"]) == "awaiting_approval"
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1


def test_required_privacy_consent_is_handed_back(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    gh_serve(server, a, extra=CONSENT_Q)
    res = run(db, tmp_path, browser, server)
    assert res["results"][0]["code"] == "unknown_questions", res
    q = handback_events(db, a["id"])[-1]["unknown_questions"]
    assert len(q) == 1 and "Privacy Notice" in q[0]
    assert server.posts == [] and status(db, a["id"]) == "awaiting_approval"


def test_required_privacy_consent_accepted_when_user_enabled_it(db, tmp_path, browser, server):
    enable(db, accept_privacy_notices=True)
    a = make_app(db)
    gh_serve(server, a, extra=CONSENT_Q)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["submitted_confirmed"] == 1, res
    assert server.posts[0]["fields"]["consent"] == "on"


def test_user_approval_is_not_used_by_worker(db, tmp_path, browser, server):
    enable(db)
    a = make_app(db)
    apps.approve(db, a["id"], "user:alex", wf.fp(db, a), user_id=ADMIN)
    gh_serve(server, a)
    res = run(db, tmp_path, browser, server)
    assert res["stats"]["considered"] == 0 and status(db, a["id"]) == "ready_to_apply" and server.gets == []
