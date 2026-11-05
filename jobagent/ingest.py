"""Normalise raw listings into employers / jobs / listings with multi-signal duplicate detection."""
from __future__ import annotations

from datetime import timedelta

from . import jd
from .db import Database, dumps, iso, parse_iso, utcnow
from .matching import ENGINEERING, NON_ENGINEERING, NON_SOFTWARE_ENGINEERING
from .normalize import (canonical_url, description_hash, job_identity, normalize_employer, normalize_title,
                        parse_location, seniority)
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


def in_scope(raw: RawJob, location: dict, settings: dict) -> tuple[bool, str]:
    title = raw.title or ""
    if NON_ENGINEERING.search(title) and not ENGINEERING.search(title):
        return False, "not an engineering role"
    if not ENGINEERING.search(title):
        return False, "title is not a software role"
    if NON_SOFTWARE_ENGINEERING.search(title):
        return False, "non-software engineering discipline"
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
                   first_discovered_at, last_seen_at, last_verified_at, status, apply_url, canonical_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
            (key, employer["id"], raw.title.strip()[:300], title_norm, req, (raw.location_raw or "")[:300] or None,
             location["country"], location["city"], jd.remote_type(raw.description_text, location, raw.remote_hint),
             jd.remote_scope(raw.description_text, raw.location_raw),
             jd.employment_type(raw.employment_type, raw.title, raw.description_text), seniority(raw.title),
             analysis["experience_years"], analysis["experience_evidence"], raw.description_text[:60000], desc_hash,
             salary_min, salary_max, currency, period, "listing" if salary_max else None,
             dumps(analysis["languages"]), analysis["education"], analysis["relocation"], raw.published_at, now, now,
             now, raw.apply_url or raw.url, curl))
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
        updates.update({"description_text": raw.description_text[:60000], "description_hash": desc_hash,
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

