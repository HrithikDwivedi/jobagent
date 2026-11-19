"""Application Companion: the browser extension that fills application forms on ANY employer site (Workday, iCIMS,
SmartRecruiters, Taleo, company portals, ... and the Greenhouse/Lever/Ashby forms auto-apply handed back).

The extension reads the form in the user's own browser tab (same label/field discovery the auto-apply drivers use),
sends the field descriptions here, and fills what comes back. Answers come from the SAME Matcher auto-apply uses, so
they only ever contain facts the user confirmed (Settings, the screening-answer library, the application's reviewed
answers, the resume). The extension never presses Submit and never solves CAPTCHAs: the user stays in control.

Authentication is a personal access token (Bearer), created and revoked by the user on the Companion page. Only its
SHA-256 hash is stored; the plain token is shown once."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from . import answers as answers_mod
from . import settings as settings_mod
from .autoapply.matcher import _EEO, RULES, Field, Matcher, norm
from .db import Database, audit, iso, loads, parse_iso, utcnow
from .resume import store as resume_store

TOKEN_PREFIX = "jac_"
TOKEN_DAYS = 90
MAX_ACTIVE_TOKENS = 5
MAX_FIELDS = 250
KINDS = {"text", "email", "tel", "url", "number", "textarea", "search", "date", "select", "radio", "checkbox",
         "checkbox_group", "combobox", "typeahead", "buttons", "file"}
# answers that come from Settings anyway (or are personal/EEO) are never "learned" into the library
PERSONAL_RULES = {"first_name", "last_name", "middle_name", "preferred_name", "full_name", "email", "phone",
                  "phone_cc", "linkedin", "github", "website", "twitter"}
SENSITIVE = re.compile(r"\b(password|passcode|ssn|social security|national insurance|aadhaar|pan (card|number)|"
                       r"passport number|date of birth|dob|bank|iban|credit card|card number|cvv|tax id)\b")


class CompanionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


# ---------------------------------------------------------------- tokens
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(db: Database, *, user_id: int, name: str, actor: str) -> tuple[str, dict]:
    name = " ".join((name or "").split())[:60] or "Browser"
    now = utcnow()
    with db.transaction():
        active = int(db.scalar("SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?",
                               (user_id, iso(now))) or 0)
        if active >= MAX_ACTIVE_TOKENS:
            raise CompanionError(f"You already have {MAX_ACTIVE_TOKENS} active tokens. Revoke one first.")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        tid = db.insert("INSERT INTO api_tokens(user_id, name, token_hash, prefix, created_at, expires_at) "
                        "VALUES (?,?,?,?,?,?)", (user_id, name, _hash(token), token[:10], iso(now),
                                                 iso(now + timedelta(days=TOKEN_DAYS))))
        audit(db, actor=actor, entity="companion", entity_id=tid, action="token_create",
              detail={"name": name, "user_id": user_id})
    return token, db.one("SELECT * FROM api_tokens WHERE id=?", (tid,))


def list_tokens(db: Database, user_id: int) -> list[dict]:
    rows = db.all("SELECT id, name, prefix, created_at, last_used_at, expires_at, revoked_at FROM api_tokens "
                  "WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,))
    now = utcnow()
    for r in rows:
        r["active"] = not r["revoked_at"] and (parse_iso(r["expires_at"]) or now) > now
    return rows


def revoke_token(db: Database, token_id: int, *, user_id: int, actor: str) -> None:
    with db.transaction():
        n = db.run("UPDATE api_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                   (iso(), token_id, user_id))
        if n != 1:
            raise CompanionError("Token not found.", 404)
        audit(db, actor=actor, entity="companion", entity_id=token_id, action="token_revoke",
              detail={"user_id": user_id})


def authenticate(db: Database, authorization: str | None) -> dict | None:
    """The approved user behind a valid Bearer token, else None."""
    m = re.fullmatch(r"Bearer\s+(\S{10,200})", (authorization or "").strip())
    if not m or not m.group(1).startswith(TOKEN_PREFIX):
        return None
    row = db.one("SELECT t.id AS token_id, t.expires_at, t.revoked_at, t.last_used_at, u.id, u.username, u.role, "
                 "u.status, u.full_name FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
                 (_hash(m.group(1)),))
    now = utcnow()
    if not row or row["revoked_at"] or (parse_iso(row["expires_at"]) or now) <= now or row["status"] != "approved":
        return None
    last = parse_iso(row["last_used_at"]) if row["last_used_at"] else None
    if not last or (now - last).total_seconds() > 60:
        with db.transaction():
            db.run("UPDATE api_tokens SET last_used_at=? WHERE id=?", (iso(now), row["token_id"]))
    return row


# ---------------------------------------------------------------- which job is this page?
def _url_key(url: str) -> tuple[str, str, dict]:
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/(apply|application|applications|apply/?)$", "", parts.path.rstrip("/").lower())
    return host, path, parse_qs(parts.query)


def url_keys(*urls: str | None) -> list[str]:
    """Normalised keys the extension matches page URLs against (host + path, and Greenhouse job ids)."""
    out = []
    for u in urls:
        if not u:
            continue
        host, path, query = _url_key(u)
        if host:
            k = host + path
            if k not in out:
                out.append(k)
        m = re.search(r"(?:gh_jid=|/jobs/)(\d{5,})", u)
        if m and "greenhouse" in (u or "") or (query.get("gh_jid") if host else None):
            gid = (query.get("gh_jid") or [m.group(1) if m else ""])[0]
            if gid and f"gh:{gid}" not in out:
                out.append(f"gh:{gid}")
    return out


def active_applications(db: Database, user_id: int, limit: int = 40) -> list[dict]:
    return db.all(
        "SELECT a.id, a.job_id, a.status, j.title, e.name AS company, j.apply_url, j.canonical_url AS url, j.country "
        "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN employers e ON e.id=j.employer_id "
        "WHERE a.user_id=? AND a.is_sample=0 AND a.status IN ('shortlisted','awaiting_approval','ready_to_apply',"
        "'application_started','submission_uncertain','submission_failed') ORDER BY a.updated_at DESC LIMIT ?",
        (user_id, limit))


def match_application(db: Database, user_id: int, url: str) -> dict | None:
    """The user's application whose job URL matches the page (same host + path, or Greenhouse gh_jid)."""
    host, path, query = _url_key(url)
    if not host:
        return None
    gh = (query.get("gh_jid") or [""])[0]
    best = None
    for a in active_applications(db, user_id, 200):
        for cand in (a.get("apply_url"), a.get("url")):
            if not cand:
                continue
            h, p, _ = _url_key(cand)
            if gh and p.endswith("/" + gh):
                return a
            if h == host and p and (p == path or path.startswith(p + "/") or p.startswith(path + "/") and len(path) > 8):
                best = best or a
    return best


# ---------------------------------------------------------------- profile + plan
def _context(db: Database, user_id: int, app_id: int | None) -> tuple[dict, dict, dict | None]:
    s = settings_mod.get_all(db, user_id)
    app = job = None
    if app_id:
        app = db.one("SELECT * FROM applications WHERE id=? AND user_id=?", (app_id, user_id))
        if not app:
            raise CompanionError("Application not found.", 404)
        job = db.one("SELECT j.*, e.name AS employer FROM jobs j JOIN employers e ON e.id=j.employer_id WHERE j.id=?",
                     (app["job_id"],))
    return s, (job or {}), app


def profile(db: Database, user_id: int) -> dict:
    s = settings_mod.get_all(db, user_id)
    c = s["candidate"] or {}
    canon = resume_store.canonical(db, user_id=user_id)
    exp = ((canon or {}).get("structured") or {}).get("experience") or []
    cur = exp[0] if exp and isinstance(exp[0], dict) else {}
    missing = [k for k in ("full_name", "email", "phone", "current_location", "notice_period", "salary_expectation")
               if not c.get(k)]
    return {"full_name": c.get("full_name"), "email": c.get("email"), "location": c.get("current_location"),
            "current_title": cur.get("title"), "current_company": cur.get("company"),
            "has_resume": bool(canon), "library_size": len(s.get("answer_library") or []),
            "missing": missing, "accept_privacy_notices": bool((s.get("auto_apply") or {}).get("accept_privacy_notices"))}


def _fields(raw: list) -> list[Field]:
    if not isinstance(raw, list):
        raise CompanionError("fields must be a list.")
    out = []
    for f in raw[:MAX_FIELDS]:
        if not isinstance(f, dict):
            continue
        kind = str(f.get("kind") or "text").lower()
        if kind not in KINDS:
            kind = "text"
        opts = [str(o)[:200] for o in (f.get("options") or [])[:200] if str(o).strip()]
        out.append(Field(key=str(f.get("key") or "")[:40], label=str(f.get("label") or "")[:500], kind=kind,
                         required=bool(f.get("required")), options=opts, name=str(f.get("name") or "")[:120],
                         hint=str(f.get("hint") or "")[:30]))
    return out


def plan(db: Database, *, user_id: int, fields: list, app_id: int | None = None) -> dict:
    """Decisions for every field in the page: {fields: [{key, action, value, source, reason, question}], summary}."""
    s, job, app = _context(db, user_id, app_id)
    cover = ""
    answers = list(s.get("answer_library") or [])
    if app:
        answers = list(loads(app["answers_json"], []) or []) + answers
        if app.get("cover_letter_id"):
            cover = db.scalar("SELECT body FROM cover_letters WHERE id=? AND user_id=?",
                              (app["cover_letter_id"], user_id)) or ""
    canon = resume_store.canonical(db, user_id=user_id)
    from .fx import rates
    matcher = Matcher({**s["candidate"], "_fx": rates(db)}, job, answers, (canon or {}).get("structured") or {},
                      s["auto_apply"], cover)
    out, filled, needs = [], 0, 0
    for f in _fields(fields):
        if SENSITIVE.search(norm(f.label)) or f.kind == "password":
            d_action, d_value, d_source, d_reason = "skip", None, "", "Sensitive field: never filled."
        else:
            d = matcher.decide(f)
            d_action, d_value, d_source, d_reason = d.action, d.value, d.source, d.reason
            if d_action == "upload_cover" and not cover:
                d_action, d_reason = "unknown", "No cover letter prepared."
        if d_action in ("fill", "choose", "type_choose", "check", "upload_resume", "upload_cover"):
            filled += 1
        elif d_action in ("unknown", "needs_consent") or (f.required and d_action == "skip"):
            needs += 1
        out.append({"key": f.key, "question": f.question, "action": d_action, "value": d_value,
                    "source": d_source, "reason": d_reason, "required": f.required})
    return {"fields": out, "summary": {"filled": filled, "needs_you": needs, "total": len(out)},
            "job": {"title": job.get("title"), "company": job.get("employer")} if job else None,
            "cover_letter": cover or None}


def resume_file(db: Database, *, user_id: int, app_id: int | None = None) -> tuple[bytes, str]:
    """PDF of the application's tailored resume, else of the canonical resume (original file if it is a PDF)."""
    from .resume import export
    s = settings_mod.get_all(db, user_id)
    parts = re.sub(r"[^A-Za-z ]", "", (s["candidate"] or {}).get("full_name") or "").split()
    stem = "_".join(parts[:3]) or "Resume"
    rv = None
    if app_id:
        app = db.one("SELECT resume_version_id FROM applications WHERE id=? AND user_id=?", (app_id, user_id))
        if not app:
            raise CompanionError("Application not found.", 404)
        if app["resume_version_id"]:
            rv = resume_store.get(db, app["resume_version_id"], user_id=user_id)
    rv = rv or resume_store.canonical(db, user_id=user_id)
    if not rv:
        raise CompanionError("Upload your resume in Resume Studio first.", 404)
    if rv.get("kind") == "original":
        f = resume_store.original_file(db, rv["id"], user_id=user_id)
        data = bytes(f["data"]) if f and f.get("data") is not None else b""
        if data.startswith(b"%PDF"):
            return data, f"{stem}_Resume.pdf"
    layout = loads(rv.get("layout_json"), None) if rv.get("layout_json") else None
    try:
        data = export.export_pdf(rv.get("structured") or {}, layout=layout)
    except TypeError:
        data = export.export_pdf(rv.get("structured") or {})
    if not data or not data.startswith(b"%PDF"):
        raise CompanionError("The resume PDF could not be generated.", 500)
    return data, f"{stem}_Resume.pdf"


# ---------------------------------------------------------------- learning + recording
def learnable(question: str) -> bool:
    q = norm(question)
    if not q or len(q) < 4 or SENSITIVE.search(q) or _EEO.search(q):
        return False
    rule = next((k for k, rx in RULES if rx.search(q)), "")
    return rule not in PERSONAL_RULES


def learn(db: Database, *, user_id: int, items: list, actor: str) -> dict:
    """Save question/answer pairs the user typed on an employer form into their screening-answer library."""
    if not isinstance(items, list):
        raise CompanionError("items must be a list.")
    keep, skipped = [], 0
    for it in items[:100]:
        if not isinstance(it, dict):
            continue
        q = " ".join(str(it.get("question", "")).split())[:300]
        a = str(it.get("answer", "")).strip()[:2000]
        if not q or not a or not learnable(q):
            skipped += 1
            continue
        keep.append({"question": q, "answer": a})
    res = answers_mod.save(db, user_id=user_id, actor=actor, items=keep) if keep else {"saved": 0, "requeued": 0}
    return {**res, "skipped": skipped}


def mark_applied(db: Database, *, user_id: int, app_id: int, page_url: str, confirmation_text: str,
                 actor: str) -> dict:
    """The user says they pressed Submit on the employer's site. Recorded as their declaration, with the page URL
    and any confirmation text the extension saw as evidence."""
    from . import applications as apps
    app = db.one("SELECT * FROM applications WHERE id=? AND user_id=?", (app_id, user_id))
    if not app:
        raise CompanionError("Application not found.", 404)
    try:
        out = apps.mark_applied(db, app_id, actor, user_id=user_id, confirmation_text=confirmation_text,
                                page_url=page_url, via="companion")
    except apps.WorkflowError as e:
        raise CompanionError(e.message, 409) from None
    audit(db, actor=actor, entity="application", entity_id=app_id, action="companion_marked_applied",
          detail={"url": (page_url or "")[:300]})
    return {"id": out["id"], "status": out["status"]}


# ---------------------------------------------------------------- AI drafts (only with an administrator grant)
def draft_answer(db: Database, ai, *, user_id: int, question: str, app_id: int | None, max_words: int = 150) -> str:
    """Draft an answer to an open question from confirmed facts only. Requires AI access (ai.available)."""
    from .ai import SYSTEM_GUARD, AIError
    if ai is None or not ai.available:
        raise CompanionError(getattr(ai, "disabled_reason", None) or "AI is not available.", 403)
    question = " ".join((question or "").split())[:500]
    if len(question) < 5:
        raise CompanionError("Question too short.")
    s, job, app = _context(db, user_id, app_id)
    canon = resume_store.canonical(db, user_id=user_id)
    st = (canon or {}).get("structured") or {}
    facts = {"candidate": {k: v for k, v in (s["candidate"] or {}).items() if k not in ("phone", "email")},
             "summary": st.get("summary"), "skills": st.get("skills"),
             "experience": (st.get("experience") or [])[:4], "education": (st.get("education") or [])[:3]}
    jd = (job.get("description_text") or "")[:4000] if job else ""
    user = (f"Draft the candidate's answer to an application-form question, first person, at most {max_words} words, "
            "plain text, using ONLY facts in <candidate_facts>. If the facts cannot support an answer, return an "
            f"empty answer.\n<question>{question}</question>\n<job_posting>{job.get('title') or ''} at "
            f"{job.get('employer') or ''}\n{jd}</job_posting>\n<candidate_facts>{facts}</candidate_facts>\n"
            'Return JSON: {"answer": "..."}')
    try:
        data = ai.json_call("companion_answer", SYSTEM_GUARD, user, 500)
    except AIError as e:
        raise CompanionError(str(e), 403 if getattr(e, "permanent", False) else 502) from None
    ans = str((data or {}).get("answer") or "").strip()
    if not ans:
        raise CompanionError("Not enough confirmed facts to draft this answer.", 422)
    return ans[:3000]


# ---------------------------------------------------------------- extension package
EXT_DIR = __import__("pathlib").Path(__file__).resolve().parent / "companion_ext"


def extension_files(server_url: str | None) -> dict[str, bytes]:
    """The unpacked extension: static files + extract.js (the auto-apply field discovery, shared verbatim) +
    defaults.js (this server's address, so the user only pastes a token)."""
    import json as _json

    from .autoapply.drivers import EXTRACT_JS
    files = {p.name: p.read_bytes() for p in sorted(EXT_DIR.iterdir()) if p.is_file()}
    files["extract.js"] = ("// Field discovery shared with JobAgent auto-apply (generated - do not edit).\n"
                           "globalThis.__jaExtract = " + EXTRACT_JS.strip() + ";\n").encode()
    files["defaults.js"] = (f"self.JA_DEFAULT_SERVER = {_json.dumps((server_url or '').rstrip('/'))};\n").encode()
    # the small script that links "Open application page" clicks to the new tab runs only on THIS JobAgent
    origin = (server_url or "").rstrip("/")
    if re.match(r"^(https://[^/\s]+|http://(127\.0\.0\.1|localhost)(:\d+)?)$", origin):
        manifest = _json.loads(files["manifest.json"])
        manifest["content_scripts"] = [{"matches": [origin + "/applications/*"], "js": ["jobagent_page.js"],
                                        "run_at": "document_idle"}]
        files["manifest.json"] = (_json.dumps(manifest, indent=2) + "\n").encode()
    return files


def extension_zip(server_url: str | None) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in extension_files(server_url).items():
            z.writestr(f"jobagent-companion/{name}", data)
    return buf.getvalue()
