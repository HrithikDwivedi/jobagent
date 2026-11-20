"""Normalise raw listings into employers / jobs / listings with multi-signal duplicate detection."""
from __future__ import annotations

import re
from datetime import timedelta

from . import jd
from .db import Database, dumps, iso, loads, parse_iso, utcnow
from .matching import ENGINEERING, NON_ENGINEERING, NON_SOFTWARE_ENGINEERING
from .normalize import (canonical_url, description_hash, job_identity, normalize_employer, normalize_title,
                        parse_location, seniority)
from . import taxonomy
from .settings import COUNTRY_NAMES, all_target_countries
from .sources import RawJob

_NAME_TO_ISO = {v.lower(): k for k, v in COUNTRY_NAMES.items()}
REOPEN_AFTER_DAYS = 45


def _country_hint(h: str | None) -> str | None:
    if not h:
        return None
    h = h.strip()
    if len(h) == 2 and h.upper() in COUNTRY_NAMES:
        return h.upper()
    return _NAME_TO_ISO.get(h.lower()) or (parse_location(h)["countries"] or [None])[0]


def resolve_location(raw: RawJob, targets: list[str]) -> dict:
    loc = parse_location(raw.location_raw)
    hint = _country_hint(raw.country_hint)
    countries = loc["countries"]
    conflict = None
    if hint and countries and hint not in countries:
        conflict = f"Structured country '{hint}' conflicts with location text '{raw.location_raw}'."
    country = hint or next((c for c in countries if c in targets), None) or (countries[0] if countries else None)
    return {"country": country, "city": loc["city"], "remote_type": loc["remote_type"], "region": loc["region"],
            "all_countries": countries, "conflict": conflict}


def source_hints(raw: RawJob) -> list[str]:
    """The source's own categorisation (department, team, category, industries, tags) for classification."""
    ex = raw.extra or {}
    out = []
    for k in ("department", "team", "category", "industry"):
        if ex.get(k):
            out.append(str(ex[k]))
    for k in ("industries", "tags"):
        v = ex.get(k)
        if isinstance(v, (list, tuple)):
            out += [str(x) for x in v[:10]]
    return out[:20]


def _legacy_software(title: str) -> tuple[bool, str]:
    if NON_ENGINEERING.search(title) and not ENGINEERING.search(title):
        return False, "not an engineering role"
    if not ENGINEERING.search(title):
        return False, "title is not a software role"
    if NON_SOFTWARE_ENGINEERING.search(title):
        return False, "non-software engineering discipline"
    return True, ""


def role_in_scope(raw: RawJob, scope: dict | None) -> tuple[bool, str]:
    """Is this title wanted by at least one user? `scope` is discovery_settings()['career_scope']; without one (or
    for users who never chose an industry) the original software-engineering filter applies."""
    title = raw.title or ""
    if not scope or (scope.get("legacy") and not scope.get("roles") and not scope.get("custom")
                     and not scope.get("industries")):
        return _legacy_software(title)
    industry, role = taxonomy.classify(title, source_hints(raw))
    if role and role in scope.get("roles", []):
        return True, f"wanted role: {taxonomy.role_label(role)}"
    if taxonomy.matches_custom(title, scope.get("custom", [])):
        return True, "matches a role typed by a user"
    if role is None and industry and industry in scope.get("industries", []):
        return True, f"wanted industry: {taxonomy.industry_label(industry)}"
    if scope.get("legacy"):
        ok, why = _legacy_software(title)
        if ok:
            return True, "software role"
    return False, "not a role any user is looking for"


def in_scope(raw: RawJob, location: dict, settings: dict) -> tuple[bool, str]:
    ok, why = role_in_scope(raw, settings.get("career_scope"))
    if not ok:
        return False, why
    targets = set(all_target_countries(settings))
    countries = set(location["all_countries"]) | ({location["country"]} if location["country"] else set())
    if countries & targets:
        return True, "target country"
    if "IN" in countries and settings.get("include_india_remote", True):
        return True, "India"
    if location["remote_type"] == "remote" and (location["region"] in ("WORLDWIDE", "EU_REGION", "EMEA_REGION")
                                                or not countries):
        return True, "remote"
    if not countries and location["region"] in ("EU_REGION", "EMEA_REGION"):
        return True, "European region"
    if not countries:
        return True, "location unknown"
    return False, "outside target countries"


def upsert_employer(db: Database, name: str, mnc_key: str | None = None) -> dict:
    from . import mnc
    norm = normalize_employer(name)
    row = db.one("SELECT * FROM employers WHERE normalized_name=?", (norm,))
    if not row:
        db.run("INSERT INTO employers(name, normalized_name, created_at) VALUES (?,?,?) "
               "ON CONFLICT(normalized_name) DO NOTHING", (name.strip()[:200] or "Unknown", norm, iso()))
        row = db.one("SELECT * FROM employers WHERE normalized_name=?", (norm,))
    if not row.get("mnc_key"):
        entry = mnc.by_key(mnc_key) if mnc_key else mnc.match_employer(name)
        if entry:
            db.run("UPDATE employers SET mnc_key=?, sector=? WHERE id=?", (entry["key"], entry["sector"], row["id"]))
            row = db.one("SELECT * FROM employers WHERE id=?", (row["id"],))
    return row


def find_equivalent(db: Database, employer_id: int, title_norm: str, country: str | None,
                    requisition_id: str | None) -> dict | None:
    """Same vacancy posted elsewhere: same employer + normalised title + country, unless both sides carry
    different requisition IDs (then they are genuinely different vacancies)."""
    if requisition_id:
        r = db.one("SELECT * FROM jobs WHERE employer_id=? AND requisition_id=?", (employer_id, requisition_id))
        if r:
            return r
    rows = db.all("SELECT * FROM jobs WHERE employer_id=? AND normalized_title=? AND COALESCE(country,'')=COALESCE(?, '') "
                  "ORDER BY id DESC", (employer_id, title_norm, country))
    for r in rows:
        if requisition_id and r["requisition_id"] and r["requisition_id"] != requisition_id:
            continue
        return r
    return None


def upsert_raw(db: Database, raw: RawJob, settings: dict, now: str | None = None) -> tuple[str, int | None, str]:
    """Insert or update one listing. Returns (decision, job_id, reason).
    decision: new | duplicate | updated | reopened | out_of_scope | invalid"""
    now = now or iso()
    if not raw.title or not raw.source_job_id:
        return "invalid", None, "missing title or id"
    targets = all_target_countries(settings)
    location = resolve_location(raw, targets)
    ok, why = in_scope(raw, location, settings)
    if not ok:
        return "out_of_scope", None, why
    closed_why = posting_closed(raw.title, raw.description_text)
    if closed_why:
        lst = db.one("SELECT job_id FROM job_listings WHERE source=? AND source_job_id=?", (raw.source, raw.source_job_id))
        if lst:
            close_job(db, lst["job_id"], closed_why, now)
            return "updated", lst["job_id"], closed_why
        return "out_of_scope", None, closed_why
    curl = canonical_url(raw.url)
    desc_hash = description_hash(raw.description_text)
    with db.transaction():
        # 1. exact listing seen before
        lst = db.one("SELECT * FROM job_listings WHERE source=? AND source_job_id=?", (raw.source, raw.source_job_id))
        if lst is None and curl:
            lst = db.one("SELECT * FROM job_listings WHERE canonical_url=?", (curl,))
        if lst:
            db.run("UPDATE job_listings SET last_seen_at=? WHERE id=?", (now, lst["id"]))
            job = db.one("SELECT * FROM jobs WHERE id=?", (lst["job_id"],))
            return _refresh_existing(db, job, raw, desc_hash, now, "same listing seen again")

        employer = upsert_employer(db, raw.company, (raw.extra or {}).get("mnc_key"))
        title_norm = normalize_title(raw.title)
        req = (raw.requisition_id or "").strip() or None
        # 2. equivalent vacancy already known (cross-posted / reposted / other board)
        job = find_equivalent(db, employer["id"], title_norm, location["country"], req)
        if job:
            closed_at = parse_iso(job["last_seen_at"])
            long_closed = (job["status"] in ("closed", "expired") and closed_at is not None
                           and utcnow() - closed_at > timedelta(days=REOPEN_AFTER_DAYS))
            if long_closed and not req and job["description_hash"] != desc_hash:
                job = None  # treat as a genuinely new vacancy (new job identity below)
            else:
                _add_listing(db, job["id"], raw, curl, now)
                return _refresh_existing(db, job, raw, desc_hash, now,
                                         "equivalent listing (same employer, title, country"
                                         + (", requisition" if req else "") + ")")

        analysis = jd.analyze(raw.title, raw.description_text)
        industry, role = taxonomy.classify(raw.title, source_hints(raw))
        key = job_identity(employer["normalized_name"], title_norm, req, location["country"], None)
        if db.one("SELECT id FROM jobs WHERE job_key=?", (key,)):
            key = job_identity(employer["normalized_name"], title_norm + "|" + (raw.published_at or now)[:10], req,
                               location["country"], None)
        sal = analysis["salary"]
        salary_min = raw.salary_min if raw.salary_min is not None else (sal or {}).get("min")
        salary_max = raw.salary_max if raw.salary_max is not None else (sal or {}).get("max")
        currency = raw.salary_currency or (sal or {}).get("currency")
        period = raw.salary_period or ((sal or {}).get("period") if sal else None)
        job_id = db.insert(
            """INSERT INTO jobs(job_key, employer_id, title, normalized_title, requisition_id, location_raw, country,
                   city, remote_type, remote_scope, employment_type, seniority, experience_min_years, experience_text,
                   description_text, description_hash, salary_min, salary_max, salary_currency, salary_period,
                   salary_source, languages_required, education_text, relocation_text, published_at,
                   first_discovered_at, last_seen_at, last_verified_at, status, apply_url, canonical_url,
                   industry, role_key, classified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?)""",
            (key, employer["id"], raw.title.strip()[:300], title_norm, req, (raw.location_raw or "")[:300] or None,
             location["country"], location["city"], jd.remote_type(raw.description_text, location, raw.remote_hint),
             jd.remote_scope(raw.description_text, raw.location_raw),
             jd.employment_type(raw.employment_type, raw.title, raw.description_text), seniority(raw.title),
             analysis["experience_years"], analysis["experience_evidence"], raw.description_text[:60000], desc_hash,
             salary_min, salary_max, currency, period, "listing" if salary_max else None,
             dumps(analysis["languages"]), analysis["education"], analysis["relocation"], raw.published_at, now, now,
             now, raw.apply_url or raw.url, curl, industry, role, now))
        _add_listing(db, job_id, raw, curl, now)
        return "new", job_id, why + (f"; {location['conflict']}" if location["conflict"] else "")


def _add_listing(db: Database, job_id: int, raw: RawJob, curl: str | None, now: str) -> None:
    if curl and db.one("SELECT id FROM job_listings WHERE canonical_url=?", (curl,)):
        curl = None  # URL already attached to a listing; keep the unique index intact
    db.run("INSERT INTO job_listings(job_id, source, source_job_id, url, canonical_url, first_seen_at, last_seen_at) "
           "VALUES (?,?,?,?,?,?,?) ON CONFLICT(source, source_job_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
           (job_id, raw.source, raw.source_job_id, raw.url, curl, now, now))


def _refresh_existing(db: Database, job: dict, raw: RawJob, desc_hash: str, now: str, reason: str):
    decision = "duplicate"
    updates = {"last_seen_at": now, "last_verified_at": now}
    if job["status"] in ("closed", "expired", "unverified"):
        updates["status"] = "open"
        decision = "reopened"
        reason = "previously closed listing is open again (same vacancy; any application is kept)"
    if desc_hash != job["description_hash"] and raw.description_text and raw.source == _primary_source(db, job["id"]):
        analysis = jd.analyze(raw.title, raw.description_text)
        industry, role = taxonomy.classify(raw.title, source_hints(raw))
        updates.update({"description_text": raw.description_text[:60000], "description_hash": desc_hash,
                        "industry": industry, "role_key": role, "classified_at": now,
                        "experience_min_years": analysis["experience_years"],
                        "experience_text": analysis["experience_evidence"],
                        "languages_required": dumps(analysis["languages"])})
        if decision == "duplicate":
            decision, reason = "updated", "listing content changed"
    sets = ", ".join(f"{k}=?" for k in updates)
    db.run(f"UPDATE jobs SET {sets} WHERE id=?", (*updates.values(), job["id"]))
    return decision, job["id"], reason


def _primary_source(db: Database, job_id: int) -> str | None:
    r = db.one("SELECT source FROM job_listings WHERE job_id=? ORDER BY id LIMIT 1", (job_id,))
    return r["source"] if r else None


def mark_closures(db: Database, source: str, board: str | None, seen_ids: set[str], now: str) -> list[int]:
    """After a COMPLETE fetch of a board, open jobs whose only listings on that board vanished are closed."""
    prefix = f"{board}:" if board else ""
    rows = db.all("SELECT l.job_id, l.source_job_id FROM job_listings l JOIN jobs j ON j.id=l.job_id "
                  "WHERE l.source=? AND j.status='open'", (source,))
    closed = []
    for r in rows:
        if prefix and not r["source_job_id"].startswith(prefix):
            continue
        if r["source_job_id"] in seen_ids:
            continue
        others = db.one("SELECT COUNT(*) AS n FROM job_listings WHERE job_id=? AND NOT (source=? AND source_job_id=?) "
                        "AND last_seen_at >= ?", (r["job_id"], source, r["source_job_id"],
                                                  iso(utcnow() - timedelta(days=3))))
        if others and others["n"] > 0:
            continue
        with db.transaction():
            n = db.run("UPDATE jobs SET status='closed', last_verified_at=? WHERE id=? AND status='open'",
                       (now, r["job_id"]))
        if n:
            closed.append(r["job_id"])
    return closed


def is_stale(job: dict, max_age_hours: int) -> bool:
    v = parse_iso(job.get("last_verified_at"))
    return v is None or utcnow() - v > timedelta(hours=max_age_hours)



TAXONOMY_KEY = "_taxonomy_version"


def reclassify_if_changed(db: Database, batch: int = 2000) -> int:
    """After the industry/role list changes, re-classify every stored job by its title (regex; seconds even for
    tens of thousands of jobs). A title that now matches a role gets that role; a job whose title matches no role
    keeps the industry it got from its source's category. Returns the number of jobs changed."""
    row = db.one("SELECT value_json FROM settings WHERE key=?", (TAXONOMY_KEY,))
    if row and loads(row["value_json"]) == taxonomy.VERSION:
        return 0
    changed, last = 0, 0
    now = iso()
    while True:
        rows = db.all("SELECT id, title, industry, role_key FROM jobs WHERE id > ? ORDER BY id LIMIT ?",
                      (last, batch))
        if not rows:
            break
        groups: dict[tuple, list[int]] = {}
        for r in rows:
            ind, role = taxonomy.classify(r["title"])
            if not role:
                if r["role_key"] and r["role_key"] not in taxonomy.ROLES:
                    role = None                                  # a role that no longer exists
                elif r["role_key"] or not ind:
                    continue                                     # keep: no better answer from the title alone
            if (ind, role) != (r["industry"], r["role_key"]):
                groups.setdefault((ind, role), []).append(r["id"])
        # one UPDATE per (industry, role): few round trips even on a remote database
        with db.transaction():
            for (ind, role), ids in groups.items():
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    db.run(f"UPDATE jobs SET industry=?, role_key=?, classified_at=? WHERE id IN "
                           f"({','.join('?' * len(chunk))})", (ind, role, now, *chunk))
                    changed += len(chunk)
        last = rows[-1]["id"]
    with db.transaction():
        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) ON CONFLICT(key) "
               "DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
               (TAXONOMY_KEY, dumps(taxonomy.VERSION), now))
    return changed


def classify_pending(db: Database, limit: int = 3000) -> int:
    """Classify jobs stored before 0007 (or by older code) into industry/role. Cheap regex; idempotent."""
    rows = db.all("SELECT id, title FROM jobs WHERE classified_at IS NULL ORDER BY id LIMIT ?", (limit,))
    if not rows:
        return 0
    now = iso()
    with db.transaction():
        for r in rows:
            ind, role = taxonomy.classify(r["title"])
            db.run("UPDATE jobs SET industry=?, role_key=?, classified_at=? WHERE id=?", (ind, role, now, r["id"]))
    return len(rows)


# ---------------------------------------------------------------- job lifecycle: closing, expiry, removal
CLOSED_TEXT = re.compile(
    r"\b(no longer (accepting|taking|open (for|to)) applications?|(this|the) (job|position|role|vacancy|posting|"
    r"opening) (has been|was|is) (closed|filled|removed|expired)|(position|vacancy|role) (has been )?filled|"
    r"applications? (are|is|have) (now )?closed|recruitment (for this (role|position) )?(is|has) closed|"
    r"(this )?(job|posting|vacancy) (has )?expired|we are no longer recruiting)\b", re.I)
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), start=1)}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
       r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DEADLINE = re.compile(r"\b(closing date|application deadline|deadline( for applications)?|apply by|applications? "
                       r"close[sd]?( on)?|closes( on)?|last date( to apply)?)\b[^0-9a-z]{0,6}(?:on\s+)?"
                       r"(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+" + _MON + r",?\s+\d{4}|" + _MON +
                       r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", re.I)
EXPIRE_UNSEEN_DAYS = 21          # an open job no source has listed for this long is treated as closed
PURGE_AFTER_DAYS = 30            # closed jobs nobody acted on are deleted after this long


def _parse_date(text: str):
    from datetime import date
    t = text.lower().replace(",", " ")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t.strip()):
            y, m, d = map(int, t.strip().split("-"))
            return date(y, m, d)
        parts = t.split()
        if parts[0].isdigit():
            d, mon, y = int(parts[0]), parts[1], int(parts[2])
        else:
            mon, d, y = parts[0], int(parts[1]), int(parts[2])
        m = next(v for k, v in _MONTHS.items() if k.startswith(mon[:3]))
        return date(y, m, d)
    except (ValueError, IndexError, StopIteration):
        return None


def closing_date(text: str | None):
    m = _DEADLINE.search(text or "")
    return _parse_date(m.group("date")) if m else None


def posting_closed(title: str | None, text: str | None, today=None) -> str | None:
    """Why the posting itself says it is closed (or None): explicit wording, or a closing date in the past."""
    from datetime import date
    body = f"{title or ''}\n{(text or '')[:20000]}"
    for m in CLOSED_TEXT.finditer(body):
        before = body[max(0, m.start() - 30):m.start()].lower()
        if re.search(r'\b(until|till|before|once|when|unless|if)\b[^.!?]*$', before):
            continue                      # "open until the position has been filled" means it is open
        return f"posting says: {m.group(0)}"
    d = closing_date(body)
    if d and d < (today or date.today()):
        return f"closing date {d.isoformat()} has passed"
    return None


def close_job(db: Database, job_id: int, reason: str, now: str | None = None, status: str = "closed") -> bool:
    """Close one shared job (every user's pre-submission application for it is closed too)."""
    from . import applications as apps
    now = now or iso()
    with db.transaction():
        n = db.run("UPDATE jobs SET status=?, last_verified_at=? WHERE id=? AND status IN ('open','unverified')",
                   (status, now, job_id))
    if n:
        apps.close_for_job(db, job_id)
        from .db import audit
        audit(db, actor="agent", entity="job", entity_id=job_id, action="job_closed", detail={"reason": reason[:200]})
    return bool(n)


def expire_unseen(db: Database, now: str | None = None, days: int = EXPIRE_UNSEEN_DAYS) -> list[int]:
    """Open jobs that no source has listed for `days` (feeds that show only recent jobs never report closures)."""
    cutoff = iso(utcnow() - timedelta(days=days))
    rows = db.all("SELECT j.id FROM jobs j WHERE j.status='open' AND j.is_sample=0 AND j.last_seen_at < ? AND NOT "
                  "EXISTS (SELECT 1 FROM job_listings l WHERE l.job_id=j.id AND l.last_seen_at >= ?)", (cutoff, cutoff))
    out = []
    for r in rows:
        if close_job(db, r["id"], f"not listed by any source for {days} days", now, status="expired"):
            out.append(r["id"])
    return out


def purge_closed(db: Database, days: int = PURGE_AFTER_DAYS) -> int:
    """Delete closed/expired jobs after `days` unless someone acted on them (an application, a bookmark, a
    tailored resume, a cover letter or a recruiter contact keeps the job for the user's history)."""
    cutoff = iso(utcnow() - timedelta(days=days))
    rows = db.all(
        "SELECT j.id FROM jobs j WHERE j.status IN ('closed','expired') AND j.is_sample=0 "
        "AND COALESCE(j.last_verified_at, j.last_seen_at) < ? "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id) "
        "AND NOT EXISTS (SELECT 1 FROM user_bookmarks b WHERE b.job_id=j.id) "
        "AND NOT EXISTS (SELECT 1 FROM resume_versions r WHERE r.job_id=j.id) "
        "AND NOT EXISTS (SELECT 1 FROM cover_letters c WHERE c.job_id=j.id) "
        "AND NOT EXISTS (SELECT 1 FROM recruiter_contacts rc WHERE rc.job_id=j.id) LIMIT 2000", (cutoff,))
    n = 0
    for r in rows:
        with db.transaction():
            for t in ("job_listings", "job_scores", "sponsorship_assessments", "run_decisions", "prep_plans"):
                db.run(f"DELETE FROM {t} WHERE job_id=?", (r["id"],))
            db.run("UPDATE outreach_messages SET job_id=NULL WHERE job_id=?", (r["id"],))
            n += db.run("DELETE FROM jobs WHERE id=?", (r["id"],))
    return n
