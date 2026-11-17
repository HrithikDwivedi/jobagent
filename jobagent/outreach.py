"""Recruiter outreach: find who is hiring for the user's open, well-matched jobs and draft a short, job-specific email.

Where contacts come from (deliberately limited to what is legitimate and reliable):
  * job_posting - email addresses the EMPLOYER printed in the job description ("contact talent@acme.com",
                  "Recruiter: Priya Sharma, priya@acme.com"). Derived on the fly, never guessed or enriched.
  * manual      - people the user found themselves (e.g. via the LinkedIn people-search link shown per job) and
                  saved with their consent-free public work address.
No scraping of LinkedIn or other networks, and no email-pattern guessing: guessed addresses bounce, hurt the
sender's reputation and break the sites' terms.

Sending: by default the draft opens in the user's own mailbox (Gmail compose or the default mail app), so it goes
from their address and replies reach them directly. When an SMTP/Resend provider is configured the server can send
it too (Reply-To = the user's email). Every outreach is logged; contacting the same address about the same job twice
needs an explicit confirmation, and server sending is capped per day."""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from .db import Database, iso, loads
from .mailer import valid_email

EMAIL_IN_TEXT = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})"
                           r"(?![\w-])")
# addresses that are not hiring contacts (privacy officers, accessibility desks, robots, ...)
NOT_HIRING = re.compile(r"^(no-?reply|do-?not-?reply|privacy|dpo|gdpr|data-?protection|legal|compliance|security|"
                        r"abuse|support|help|info-?security|accommodations?|accessibility|disability|ada|"
                        r"benefits|payroll|fraud|ethics|whistle\w*|press|media|investor\w*)\b", re.I)
HIRING_HINT = re.compile(r"recruit|talent|career|hiring|jobs?|hr\b|people|staffing|apply|resum|cv", re.I)
NAME_NEAR = re.compile(r"(?i:recruiter|talent (?:partner|acquisition(?: partner| specialist)?)|hiring manager|"
                       r"point of contact|contact person|contact)\s*[:\-–]\s*([A-Z][a-z'\-]+(?: [A-Z][a-z'\-]+){0,2})")
SERVER_DAILY_CAP = 25
MAX_POSTING_JOBS = 400


def extract_contacts(text: str) -> list[dict]:
    """Hiring contacts printed in a job description: [{email, name, kind}] (kind: recruiter|generic)."""
    text = text or ""
    names = NAME_NEAR.findall(text)
    out, seen = [], set()
    for m in EMAIL_IN_TEXT.finditer(text):
        email = m.group(1).strip(".").lower()
        local = email.split("@", 1)[0]
        if email in seen or not valid_email(email) or NOT_HIRING.match(local):
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|webp)$", email):
            continue
        seen.add(email)
        personal = bool(re.search(r"[a-z]+[._-][a-z]+", local)) and not HIRING_HINT.search(local)
        # the nearest name just before it, unless another address sits in between (that name belongs to it)
        pre = text[max(0, m.start() - 80):m.start()]
        near = [n for n in NAME_NEAR.finditer(pre) if "@" not in pre[n.end():]]
        name = near[-1].group(1) if near else (names[0] if personal and len(names) == 1 else None)
        out.append({"email": email, "name": name if personal or name else None,
                    "kind": "recruiter" if personal or name else "generic"})
    return out


def _job_rows(db: Database, user_id: int, min_score: float, job_id: int | None = None) -> list[dict]:
    where = ["j.status='open'", "j.is_sample=0"]
    params: list = [user_id]
    if job_id is not None:
        where.append("j.id=?")
        params.append(job_id)
    else:
        where.append("COALESCE(sc.score, 0) >= ?")
        params.append(min_score)
    return db.all(
        "SELECT j.id, j.title, j.location_raw, j.country, j.city, j.description_text, j.canonical_url, j.apply_url, "
        "e.name AS employer, sc.score, sc.details_json "
        "FROM jobs j JOIN employers e ON e.id=j.employer_id "
        "LEFT JOIN job_scores sc ON sc.id = (SELECT MAX(id) FROM job_scores WHERE job_id=j.id AND user_id=?) "
        "WHERE " + " AND ".join(where) + " ORDER BY COALESCE(sc.score, 0) DESC, j.id DESC LIMIT ?",
        params + [MAX_POSTING_JOBS])


def hiring_board(db: Database, *, user_id: int, min_score: float = 60, company: str = "") -> list[dict]:
    """Companies currently hiring for the user's matched jobs, with every known contact per job.
    Returns [{job..., contacts: [...], last_outreach}] for jobs that have at least one contact, plus jobs without
    contacts (so the user can add one), best matches first."""
    jobs = _job_rows(db, user_id, min_score)
    manual = db.all("SELECT * FROM recruiter_contacts WHERE user_id=? ORDER BY id DESC", (user_id,))
    by_job: dict[int, list[dict]] = {}
    by_company: dict[str, list[dict]] = {}
    for c in manual:
        c = dict(c, kind="recruiter" if c.get("name") else "generic")
        if c.get("job_id"):
            by_job.setdefault(c["job_id"], []).append(c)
        else:
            by_company.setdefault((c["company"] or "").strip().lower(), []).append(c)
    sent = {}
    for r in db.all("SELECT job_id, to_addr, MAX(created_at) AS at, COUNT(*) AS n FROM outreach_messages "
                    "WHERE user_id=? GROUP BY job_id, to_addr", (user_id,)):
        sent[(r["job_id"], r["to_addr"].lower())] = r
    q = company.strip().lower()
    out = []
    for j in jobs:
        if q and q not in (j["employer"] or "").lower() and q not in (j["title"] or "").lower():
            continue
        contacts = [dict(c, source="job_posting", id=None) for c in extract_contacts(j["description_text"])]
        known = {c["email"] for c in contacts}
        for c in by_job.get(j["id"], []) + by_company.get((j["employer"] or "").strip().lower(), []):
            if c["email"].lower() not in known:
                contacts.append(c)
                known.add(c["email"].lower())
        for c in contacts:
            s = sent.get((j["id"], c["email"].lower()))
            c["contacted_at"], c["contacted_n"] = (s["at"], s["n"]) if s else (None, 0)
        out.append({"id": j["id"], "title": j["title"], "employer": j["employer"], "score": j["score"],
                    "location": j["location_raw"] or ", ".join(x for x in (j["city"], j["country"]) if x),
                    "url": j["canonical_url"] or j["apply_url"], "contacts": contacts,
                    "linkedin_search": linkedin_people_search(j["employer"], j["title"])})
    out.sort(key=lambda r: (not r["contacts"], -(r["score"] or 0)))
    return out


def linkedin_people_search(company: str, title: str) -> str:
    """A LinkedIn people search the USER runs themselves (we never fetch it)."""
    family = "engineering" if re.search(r"engineer|developer|software|data|ml|ai\b|devops|sre", title or "", re.I) \
        else "hiring"
    return "https://www.linkedin.com/search/results/people/?" + urlencode(
        {"keywords": f'{company} technical recruiter OR talent acquisition {family}'.strip()})


def job_for(db: Database, job_id: int, *, user_id: int) -> dict | None:
    rows = _job_rows(db, user_id, 0, job_id)
    return rows[0] if rows else None


def matched_skills(details_json: str | None, limit: int = 4) -> list[str]:
    det = loads(details_json, {}) or {}
    skills = [d.get("skill") for d in (det.get("must_have") or []) + (det.get("nice_to_have") or [])
              if isinstance(d, dict) and (d.get("credit") or 0) >= 1.0 and d.get("skill")]
    out = []
    for s in skills:
        if s not in out:
            out.append(s)
    return out[:limit]


def draft(job: dict, candidate: dict, contact_name: str | None = None, current_title: str | None = None) -> tuple[str, str]:
    """A short, specific, honest first email built only from Settings, the canonical resume's current title and the
    job's own match data. The user edits it before anything is sent."""
    full = (candidate.get("full_name") or "").strip()
    first = (contact_name or "").split()[0] if contact_name else ""
    title, company = job["title"], job["employer"]
    skills = matched_skills(job.get("details_json"))
    subject = f"{title} at {company}" + (f" – {full}" if full else "")
    where = f" ({job['location_raw']})" if job.get("location_raw") else ""
    lines = [f"Hi {first}," if first else "Hello,", "",
             f"I'm reaching out about the {title} role at {company}{where}."]
    me = f"I'm currently working as a {current_title}" if current_title else ""
    if skills:
        skill_txt = ", ".join(skills[:-1]) + (" and " if len(skills) > 1 else "") + skills[-1]
        lines.append((me + ", and my" if me else "My") + f" day-to-day work with {skill_txt} lines up closely "
                     "with what the role asks for.")
    elif me:
        lines.append(me + ".")
    lines += ["", "I've applied (or am about to apply) through the official listing and would really appreciate a "
                  "few minutes to learn more about the team, or a pointer to the right person if that isn't you.",
              "", "Thank you for your time.", "", "Best regards,"]
    if full:
        lines.append(full)
    contact = [x for x in (candidate.get("phone"), candidate.get("email"), candidate.get("linkedin_url")) if x]
    if contact:
        lines.append(" | ".join(contact))
    return subject[:200], "\n".join(lines).strip() + "\n"


def gmail_url(to: str, subject: str, body: str) -> str:
    return "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject,
                                                          "body": body})


def mailto_url(to: str, subject: str, body: str) -> str:
    return f"mailto:{quote(to, safe='@')}?subject={quote(subject)}&body={quote(body)}"


def previous(db: Database, *, user_id: int, job_id: int | None, to_addr: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM outreach_messages WHERE user_id=? AND to_addr=? AND "
                         + ("job_id=?" if job_id else "job_id IS NULL"),
                         (user_id, to_addr.lower()) + ((job_id,) if job_id else ())) or 0)


def server_sent_today(db: Database, *, user_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM outreach_messages WHERE user_id=? AND channel='server' AND "
                         "status='sent' AND created_at >= ?", (user_id, iso()[:10])) or 0)


def log(db: Database, *, user_id: int, job_id: int | None, to_addr: str, to_name: str | None, company: str | None,
        subject: str, body: str, channel: str, status: str, error: str | None = None) -> int:
    with db.transaction():
        return db.insert("INSERT INTO outreach_messages(user_id, job_id, to_addr, to_name, company, subject, body_text, "
                         "channel, status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (user_id, job_id, to_addr.lower()[:320], (to_name or "")[:120] or None, (company or "")[:200],
                          subject[:300], body[:10000], channel, status, (error or "")[:300] or None, iso()))


def history(db: Database, *, user_id: int, limit: int = 50) -> list[dict]:
    return db.all("SELECT o.*, j.title FROM outreach_messages o LEFT JOIN jobs j ON j.id=o.job_id WHERE o.user_id=? "
                  "ORDER BY o.id DESC LIMIT ?", (user_id, limit))


def add_contact(db: Database, *, user_id: int, company: str, email: str, name: str = "", role_title: str = "",
                profile_url: str = "", job_id: int | None = None, notes: str = "") -> int:
    email = email.strip().lower()
    if not valid_email(email):
        raise ValueError("Enter a valid email address.")
    if not company.strip():
        raise ValueError("Enter the company.")
    if profile_url and not re.match(r"^https://", profile_url):
        raise ValueError("The profile link must start with https://")
    if db.scalar("SELECT COUNT(*) FROM recruiter_contacts WHERE user_id=? AND email=? AND "
                 + ("job_id=?" if job_id else "job_id IS NULL"), (user_id, email) + ((job_id,) if job_id else ())):
        raise ValueError("That contact is already saved.")
    with db.transaction():
        return db.insert("INSERT INTO recruiter_contacts(user_id, job_id, company, name, email, role_title, profile_url, "
                         "source, notes, created_at) VALUES (?,?,?,?,?,?,?, 'manual', ?, ?)",
                         (user_id, job_id, company.strip()[:200], name.strip()[:120] or None, email,
                          role_title.strip()[:120] or None, profile_url.strip()[:500] or None, notes.strip()[:1000] or None,
                          iso()))


def delete_contact(db: Database, *, user_id: int, contact_id: int) -> bool:
    with db.transaction():
        return bool(db.run("DELETE FROM recruiter_contacts WHERE id=? AND user_id=?", (contact_id, user_id)))
