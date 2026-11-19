"""Help & Support: users raise a concern, the administrator answers it.

Before a ticket is raised the user sees answers that already exist for their question:
  * built-in help articles (KB below, maintained with the code),
  * answers the administrator flagged as reusable ("Save as a suggested answer" on a reply, or written directly),
  * live checks of the user's OWN account (missing resume, unanswered screening questions, auto-apply off, ...),
so most questions are solved without waiting. Tickets are private to their author and the administrator."""
from __future__ import annotations

import re
from collections import Counter

from .db import Database, audit, iso, utcnow

CATEGORIES: dict[str, str] = {
    "auto_apply": "Auto-apply & manual forms",
    "companion": "Application Companion",
    "ai_access": "AI access",
    "jobs": "Jobs & matching",
    "resume": "Resume & cover letters",
    "outreach": "Recruiter outreach",
    "account": "Account & sign-in",
    "bug": "Something is broken",
    "other": "Other",
}
STATUSES = {"open": "Open", "answered": "Answered", "resolved": "Resolved", "closed": "Closed"}
ACTIVE = ("open", "answered")
MAX_OPEN_PER_USER = 10
MAX_PER_DAY = 20
SUBJECT_MIN, SUBJECT_MAX = 4, 150
BODY_MIN, BODY_MAX = 10, 5000


class SupportError(Exception):
    pass


# ---------------------------------------------------------------- built-in answers
KB: list[dict] = [
    {"id": "kb-manual-forms", "category": "auto_apply",
     "question": "Why does auto-apply still ask me to fill application forms manually?",
     "keywords": "manual fill form handed back hand back awaiting approval auto apply stuck not submitting",
     "answer": "Auto-apply hands an application back when a REQUIRED question has no confirmed answer, a visible "
               "CAPTCHA appears, or the site is not Greenhouse, Lever or Ashby. Open Screening Answers: every blocking "
               "question is listed there. Answer each once and the affected applications are re-queued automatically. "
               "For any other site (Workday, iCIMS, SmartRecruiters, company portals) use the Application Companion "
               "browser extension to fill the form in one click."},
    {"id": "kb-privacy-box", "category": "auto_apply",
     "question": "Auto-apply stops at a privacy / consent checkbox",
     "keywords": "privacy consent gdpr checkbox tick agree data processing acknowledge",
     "answer": "Required privacy or data-processing boxes are never ticked unless you allow it. On Screening Answers "
               "switch on 'Tick required privacy boxes for me' (or Settings → Auto-apply). Optional consents and "
               "marketing boxes always stay unticked."},
    {"id": "kb-autoapply-off", "category": "auto_apply",
     "question": "Auto-apply never runs / nothing is submitted",
     "keywords": "auto apply not running disabled enable schedule nothing happens github actions",
     "answer": "Auto-apply is off until you enable it in Settings → Auto-apply and acknowledge the policy. It only "
               "applies to jobs at or above the minimum score (default 75) on supported sites, within your daily "
               "limit. Lower 'Minimum score' or widen the countries if no job qualifies. The scheduled run happens "
               "every 3 hours."},
    {"id": "kb-captcha", "category": "auto_apply",
     "question": "The application has a CAPTCHA / 'verify you are human'",
     "keywords": "captcha recaptcha hcaptcha human verification robot cloudflare",
     "answer": "Invisible CAPTCHAs no longer stop auto-apply. A VISIBLE challenge always needs a person: open the "
               "application, use the Application Companion to fill everything, solve the challenge yourself and "
               "press Submit. JobAgent never tries to solve CAPTCHAs."},
    {"id": "kb-companion-what", "category": "companion",
     "question": "How do I fill an application form on Workday / any other site automatically?",
     "keywords": "companion extension chrome workday icims taleo smartrecruiters fill form browser keyboard mouse",
     "answer": "Install the Application Companion (left menu → Application Companion): download the extension, load "
               "it in Chrome/Edge (Extensions → Developer mode → Load unpacked), create an access token and paste it "
               "into the extension. On any application page press 'Fill this form': it fills your details and saved "
               "answers, attaches your resume and highlights what still needs you. You always press Submit yourself."},
    {"id": "kb-companion-token", "category": "companion",
     "question": "The Companion says 'token invalid' or 'not connected'",
     "keywords": "companion token invalid expired not connected 401 unauthorized extension error",
     "answer": "Tokens expire after 90 days and stop working when revoked or when your account is suspended. Open "
               "Application Companion in JobAgent, create a new token and paste it into the extension's options page. "
               "Check that the server address in the options is exactly your JobAgent URL (https://...)."},
    {"id": "kb-companion-learn", "category": "companion",
     "question": "Can the Companion remember answers I type myself?",
     "keywords": "remember save answers learn library reuse companion typed",
     "answer": "Yes. After you fill the remaining questions yourself, press 'Save my answers' in the Companion. It shows "
               "the new question/answer pairs, you untick anything private, and they go into your Screening Answers "
               "library, which both the Companion and auto-apply reuse."},
    {"id": "kb-ai-access", "category": "ai_access",
     "question": "How do I get AI features (AI resume polish, cover letters, answer drafts)?",
     "keywords": "ai access enable openai gpt artificial intelligence cover letter draft polish disabled",
     "answer": "AI features are enabled per account by the administrator; new accounts start without them and use the "
               "rule-based tools, which work fully. Raise a request with the category 'AI access' and the "
               "administrator can switch it on (optionally with a monthly allowance)."},
    {"id": "kb-ai-limit", "category": "ai_access",
     "question": "AI says my monthly allowance would be exceeded",
     "keywords": "ai budget allowance limit exceeded monthly spend",
     "answer": "Your account has a monthly AI allowance set by the administrator, and the installation also has an "
               "overall monthly budget. Everything keeps working without AI. Ask for a higher allowance under "
               "Help & Support if you need it before the month resets."},
    {"id": "kb-no-jobs", "category": "jobs",
     "question": "I see no jobs or very few recommendations",
     "keywords": "no jobs empty few recommendations matches score filter countries",
     "answer": "Jobs need your resume to be scored. Upload it in Resume Studio, then check Settings → Search: target "
               "countries, preferred roles, excluded title words (e.g. 'senior') and the minimum match score. New "
               "jobs arrive with each agent run."},
    {"id": "kb-sponsorship", "category": "jobs",
     "question": "How is visa sponsorship decided for a job?",
     "keywords": "visa sponsorship sponsor skilled worker register likely confirmed",
     "answer": "Sponsorship is 'confirmed' when the posting says so, 'likely' from the official UK sponsor register or "
               "strong signals, and 'not supported' when the posting rules it out. The evidence is shown on each job "
               "under Sponsorship & Relocation. Set what you accept in Settings → Search."},
    {"id": "kb-resume-upload", "category": "resume",
     "question": "My resume upload fails or looks wrong",
     "keywords": "resume upload failed pdf docx parse scanned image wrong",
     "answer": "Upload a text-based PDF or DOCX under 10 MB (scanned images cannot be read). After upload, check the "
               "parsed sections in Resume Studio and correct facts in Settings → Candidate; tailored versions are "
               "always built from your confirmed facts only."},
    {"id": "kb-cover-letter", "category": "resume",
     "question": "How do I edit or regenerate a cover letter?",
     "keywords": "cover letter edit regenerate change letter draft",
     "answer": "Open the application, edit the cover letter text and save: a new version is created and the "
               "application needs a fresh review before approval."},
    {"id": "kb-recruiters", "category": "outreach",
     "question": "How do I email a recruiter about a job?",
     "keywords": "recruiter email hr outreach contact mail hiring manager",
     "answer": "Open Recruiters (or 'Contact recruiters' on a job). Contacts printed in job posts are listed "
               "automatically; you can add your own. Compose opens a fact-based draft you can send from Gmail, your "
               "mail app, or from JobAgent if email is configured."},
    {"id": "kb-password", "category": "account",
     "question": "How do I change my password or sign out of other devices?",
     "keywords": "password change reset forgot sign out devices session",
     "answer": "Settings → Password changes it and signs out every other session. If you forgot it, raise a ticket "
               "from another signed-in device or contact the administrator directly."},
    {"id": "kb-data", "category": "account",
     "question": "Who can see my data? Can I export or delete it?",
     "keywords": "privacy data export delete account who can see admin",
     "answer": "Your resumes, applications and settings are private to your account; the administrator sees account "
               "details, counts and the tickets you raise. Settings → Data exports your settings; ask the "
               "administrator to delete the account and all of its data."},
]

STOP = set("a an and are as at be but by can do does for from get got have how i if in into is it its me my no not "
           "of on or so that the then there this to was what when where which why will with you your our we "
           "please help issue problem still".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 1]


def _score(query: Counter, doc: dict) -> float:
    q_tokens = set(query)
    title = set(tokens(doc["question"]))
    keys = set(tokens(doc.get("keywords", "")))
    body = set(tokens(doc["answer"]))
    s = 0.0
    for t in q_tokens:
        if t in title:
            s += 3
        if t in keys:
            s += 2
        if t in body:
            s += 0.5
    return s / (1 + 0.15 * len(q_tokens))


def flagged_answers(db: Database, category: str | None = None) -> list[dict]:
    sql = "SELECT * FROM support_answers"
    params: tuple = ()
    if category:
        sql += " WHERE category=?"
        params = (category,)
    return db.all(sql + " ORDER BY helpful DESC, id DESC", params)


def suggest(db: Database, query: str, category: str = "", limit: int = 5) -> list[dict]:
    """Best existing answers for a free-text question: administrator-flagged answers first on ties, then
    built-in articles. Each item: {id, source ('team'|'help'), category, question, answer, score}."""
    q = Counter(tokens(query))
    docs = [{**a, "id": f"team-{a['id']}", "source": "team"} for a in flagged_answers(db)] + \
           [{**a, "source": "help"} for a in KB]
    out = []
    for d in docs:
        s = _score(q, d) if q else 0.0
        if category and d["category"] == category:
            s += 1.0 if q else 2.0
        if d["source"] == "team":
            s += 0.4 + min(int(d.get("helpful") or 0), 10) * 0.05
        if s >= 1.2:
            out.append({"id": d["id"], "source": d["source"], "category": d["category"], "question": d["question"],
                        "answer": d["answer"], "score": round(s, 2)})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---------------------------------------------------------------- account self-check
def diagnose(db: Database, user_id: int) -> list[dict]:
    """Live checks of the user's own setup that explain most support questions. [{level, title, detail, href}]"""
    from . import answers as answers_mod
    from . import settings as settings_mod
    from .ai import ai_access
    from .resume import store as resume_store
    out: list[dict] = []
    s = settings_mod.get_all(db, user_id)
    if not resume_store.canonical(db, user_id=user_id):
        out.append({"level": "warn", "title": "No resume uploaded",
                    "detail": "Jobs cannot be scored or applied to until you upload your resume.", "href": "/studio"})
    cand = s.get("candidate") or {}
    missing = [lbl for k, lbl in (("full_name", "full name"), ("email", "email"), ("phone", "phone"),
                                  ("current_location", "current location")) if not cand.get(k)]
    if missing:
        out.append({"level": "warn", "title": "Candidate details incomplete",
                    "detail": "Forms need your " + ", ".join(missing) + ". Add them in Settings → Candidate.",
                    "href": "/settings#candidate"})
    elif not cand.get("notice_period") or not cand.get("salary_expectation"):
        out.append({"level": "info", "title": "Notice period / salary not set",
                    "detail": "Many forms ask for these; without them those questions are handed back to you.",
                    "href": "/settings#candidate"})
    try:
        ov = answers_mod.overview(db, user_id=user_id)
    except Exception:
        ov = {"pending": [], "reasons": []}
    if ov["pending"]:
        n_apps = len({a for p in ov["pending"] for a in p["apps"]})
        out.append({"level": "warn", "title": f"{len(ov['pending'])} screening question(s) need an answer",
                    "detail": f"They are holding back {n_apps} application(s). Answer them once and those "
                              "applications are retried automatically.", "href": "/answers"})
    if any(p.get("consent") for p in ov["pending"]) and not (s.get("auto_apply") or {}).get("accept_privacy_notices"):
        out.append({"level": "warn", "title": "Required privacy boxes are not allowed",
                    "detail": "Some forms need a privacy acknowledgement. Allow it on Screening Answers.",
                    "href": "/answers"})
    pol = s.get("auto_apply") or {}
    if not (pol.get("enabled") and pol.get("acknowledged_at")):
        out.append({"level": "info", "title": "Auto-apply is off",
                    "detail": "Turn it on in Settings → Auto-apply to submit qualifying applications for you.",
                    "href": "/settings#auto_apply"})
    acc = ai_access(db, user_id)
    if not acc["allowed"]:
        out.append({"level": "info", "title": "AI features are not enabled for your account",
                    "detail": "Rule-based tools are used. Request AI access below if you need it.",
                    "href": "/support?category=ai_access"})
    elif acc["limit"] is not None and acc["spent"] >= acc["limit"] * 0.9:
        out.append({"level": "warn", "title": "AI allowance almost used",
                    "detail": f"${acc['spent']:.2f} of ${acc['limit']:.2f} used this month.", "href": "/support"})
    if not db.scalar("SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?",
                     (user_id, iso())):
        out.append({"level": "info", "title": "Application Companion not connected",
                    "detail": "Fill forms on Workday, iCIMS and any other site in one click with the browser extension.",
                    "href": "/companion"})
    return out


# ---------------------------------------------------------------- tickets
def _clean(text: str, max_len: int) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\x00", "").strip()
    return text[:max_len]


def create_ticket(db: Database, *, user_id: int, category: str, subject: str, body: str, actor: str) -> int:
    category = category if category in CATEGORIES else "other"
    subject = " ".join(_clean(subject, SUBJECT_MAX + 50).split())
    body = _clean(body, BODY_MAX + 1)
    if len(subject) < SUBJECT_MIN or len(subject) > SUBJECT_MAX:
        raise SupportError(f"Give the request a short subject ({SUBJECT_MIN}-{SUBJECT_MAX} characters).")
    if len(body) < BODY_MIN or len(body) > BODY_MAX:
        raise SupportError(f"Describe the problem in {BODY_MIN}-{BODY_MAX} characters.")
    since = iso(utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
    with db.transaction():
        if int(db.scalar("SELECT COUNT(*) FROM support_tickets WHERE user_id=? AND status IN ('open','answered')",
                         (user_id,)) or 0) >= MAX_OPEN_PER_USER:
            raise SupportError(f"You already have {MAX_OPEN_PER_USER} open requests. Resolve one first.")
        if int(db.scalar("SELECT COUNT(*) FROM support_tickets WHERE user_id=? AND created_at >= ?",
                         (user_id, since)) or 0) >= MAX_PER_DAY:
            raise SupportError("Too many requests today. Try again tomorrow.")
        now = iso()
        tid = db.insert("INSERT INTO support_tickets(user_id, category, subject, status, created_at, updated_at, "
                        "last_user_at, user_seen_at) VALUES (?,?,?,'open',?,?,?,?)",
                        (user_id, category, subject, now, now, now, now))
        db.run("INSERT INTO support_messages(ticket_id, author_id, is_staff, body, created_at) VALUES (?,?,0,?,?)",
               (tid, user_id, body, now))
        audit(db, actor=actor, entity="support", entity_id=tid, action="ticket_open",
              detail={"category": category, "user_id": user_id})
    return tid


def get_ticket(db: Database, tid: int, *, user_id: int | None) -> dict | None:
    """The ticket with its messages. user_id=None means administrator access (any ticket)."""
    sql = ("SELECT t.*, u.username, u.full_name, u.email, u.ai_access, u.role FROM support_tickets t "
           "JOIN users u ON u.id=t.user_id WHERE t.id=?")
    params: tuple = (tid,)
    if user_id is not None:
        sql += " AND t.user_id=?"
        params = (tid, user_id)
    t = db.one(sql, params)
    if not t:
        return None
    t["messages"] = db.all("SELECT m.*, u.username, u.full_name FROM support_messages m "
                           "LEFT JOIN users u ON u.id=m.author_id WHERE m.ticket_id=? ORDER BY m.id", (tid,))
    return t


def list_for_user(db: Database, user_id: int) -> list[dict]:
    return db.all("SELECT t.*, (SELECT COUNT(*) FROM support_messages m WHERE m.ticket_id=t.id) AS n_messages, "
                  "CASE WHEN t.last_staff_at IS NOT NULL AND t.user_seen_at IS NULL THEN 1 ELSE 0 END AS unread FROM support_tickets t WHERE t.user_id=? "
                  "ORDER BY CASE WHEN t.status IN ('open','answered') THEN 0 ELSE 1 END, t.updated_at DESC", (user_id,))


def list_all(db: Database, status: str = "active", category: str = "", q: str = "") -> list[dict]:
    where, params = [], []
    if status == "active":
        where.append("t.status IN ('open','answered')")
    elif status in STATUSES:
        where.append("t.status=?")
        params.append(status)
    if category in CATEGORIES:
        where.append("t.category=?")
        params.append(category)
    if q.strip():
        where.append("(LOWER(t.subject) LIKE ? OR LOWER(u.username) LIKE ?)")
        like = f"%{q.strip().lower()[:80]}%"
        params += [like, like]
    sql = ("SELECT t.*, u.username, u.full_name, (SELECT COUNT(*) FROM support_messages m WHERE m.ticket_id=t.id) "
           "AS n_messages FROM support_tickets t JOIN users u ON u.id=t.user_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END, t.updated_at DESC "
            "LIMIT 300")
    return db.all(sql, tuple(params))


def counts(db: Database) -> dict:
    rows = db.all("SELECT status, COUNT(*) AS n FROM support_tickets GROUP BY status")
    out = {k: 0 for k in STATUSES}
    for r in rows:
        out[r["status"]] = int(r["n"])
    return out


def unread_for_user(db: Database, user_id: int) -> int:
    # a staff reply clears user_seen_at; opening the request sets it again (no timestamp comparison: timestamps
    # have one-second resolution)
    return int(db.scalar("SELECT COUNT(*) FROM support_tickets WHERE user_id=? AND last_staff_at IS NOT NULL AND "
                         "user_seen_at IS NULL", (user_id,)) or 0)


def mark_seen(db: Database, tid: int, user_id: int) -> None:
    with db.transaction():
        db.run("UPDATE support_tickets SET user_seen_at=? WHERE id=? AND user_id=?", (iso(), tid, user_id))


def reply(db: Database, tid: int, *, author_id: int, staff: bool, body: str, actor: str,
          resolve: bool = False) -> dict:
    """Add a message. A staff reply marks the ticket 'answered' (or 'resolved'); a user reply re-opens it."""
    body = _clean(body, BODY_MAX + 1)
    if len(body) < 2 or len(body) > BODY_MAX:
        raise SupportError(f"Write a reply of up to {BODY_MAX} characters.")
    with db.transaction():
        t = db.one("SELECT * FROM support_tickets WHERE id=?", (tid,))
        if not t or (not staff and int(t["user_id"]) != author_id):
            raise SupportError("Request not found.")
        if t["status"] == "closed" and not staff:
            raise SupportError("This request is closed. Raise a new one.")
        now = iso()
        db.run("INSERT INTO support_messages(ticket_id, author_id, is_staff, body, created_at) VALUES (?,?,?,?,?)",
               (tid, author_id, int(staff), body, now))
        if staff:
            new = "resolved" if resolve else "answered"
            db.run("UPDATE support_tickets SET status=?, updated_at=?, last_staff_at=?, user_seen_at=NULL WHERE id=?",
                   (new, now, now, tid))
        else:
            new = "open"
            db.run("UPDATE support_tickets SET status='open', updated_at=?, last_user_at=?, user_seen_at=? WHERE id=?",
                   (now, now, now, tid))
        audit(db, actor=actor, entity="support", entity_id=tid, action="ticket_reply",
              from_status=t["status"], to_status=new, detail={"staff": staff, "user_id": t["user_id"]})
    return db.one("SELECT * FROM support_tickets WHERE id=?", (tid,))


def set_status(db: Database, tid: int, status: str, *, actor: str, user_id: int | None) -> None:
    """Users may resolve or re-open their own request; the administrator may set any status."""
    if status not in STATUSES:
        raise SupportError("Unknown status.")
    with db.transaction():
        t = db.one("SELECT * FROM support_tickets WHERE id=?", (tid,))
        if not t or (user_id is not None and int(t["user_id"]) != user_id):
            raise SupportError("Request not found.")
        if user_id is not None and status not in ("resolved", "open"):
            raise SupportError("You can mark a request resolved or re-open it.")
        if user_id is not None and t["status"] == "closed":
            raise SupportError("This request is closed. Raise a new one.")
        db.run("UPDATE support_tickets SET status=?, updated_at=? WHERE id=?", (status, iso(), tid))
        audit(db, actor=actor, entity="support", entity_id=tid, action="ticket_status",
              from_status=t["status"], to_status=status, detail={"user_id": t["user_id"]})


# ---------------------------------------------------------------- administrator-flagged answers
def save_answer(db: Database, *, actor: str, admin_id: int | None, category: str, question: str, answer: str,
                keywords: str = "", source_ticket_id: int | None = None, answer_id: int | None = None) -> int:
    category = category if category in CATEGORIES else "other"
    question = " ".join(_clean(question, 300).split())
    answer = _clean(answer, 4000)
    keywords = " ".join(_clean(keywords, 300).split())
    if len(question) < 4 or len(answer) < 10:
        raise SupportError("A suggested answer needs a question (4+ characters) and an answer (10+ characters).")
    now = iso()
    with db.transaction():
        if answer_id:
            n = db.run("UPDATE support_answers SET category=?, question=?, answer=?, keywords=?, updated_at=? WHERE id=?",
                       (category, question, answer, keywords, now, answer_id))
            if n != 1:
                raise SupportError("Answer not found.")
            aid = answer_id
        else:
            if int(db.scalar("SELECT COUNT(*) FROM support_answers") or 0) >= 500:
                raise SupportError("The answer library is full (500). Delete unused answers first.")
            aid = db.insert("INSERT INTO support_answers(category, question, answer, keywords, source_ticket_id, "
                            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                            (category, question, answer, keywords, source_ticket_id, admin_id, now, now))
        audit(db, actor=actor, entity="support", entity_id=f"answer-{aid}",
              action="answer_update" if answer_id else "answer_flag", detail={"category": category})
    return aid


def delete_answer(db: Database, answer_id: int, *, actor: str) -> None:
    with db.transaction():
        db.run("DELETE FROM support_answers WHERE id=?", (answer_id,))
        audit(db, actor=actor, entity="support", entity_id=f"answer-{answer_id}", action="answer_delete")


def mark_helpful(db: Database, answer_ref: str) -> None:
    m = re.fullmatch(r"team-(\d+)", answer_ref or "")
    if m:
        with db.transaction():
            db.run("UPDATE support_answers SET helpful=helpful+1 WHERE id=?", (int(m.group(1)),))
