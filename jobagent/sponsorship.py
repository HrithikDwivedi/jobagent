"""Evidence-based sponsorship classification.

confirmed      explicit statement in THIS job posting that visa sponsorship / work-permit support is offered
likely         credible pathway evidence (e.g. employer is on the UK Home Office licensed-sponsor register for a
               UK role, or posting explicitly welcomes international applicants with relocation support) but no
               position-specific confirmation
unknown        insufficient or conflicting evidence
not_supported  posting says sponsorship is unavailable / existing right to work is required

Company size or fame is never used as evidence."""
from __future__ import annotations

import csv
import gzip
import io
import re

from .db import Database, iso, parse_iso, utcnow
from .http import FetchError, HttpClient
from .normalize import normalize_employer

NEGATIVE = [
    r"(?:unable|not able|cannot|can't|can ?not|won't|will not|do not|don't|does not|doesn't)\s+(?:to\s+)?"
    r"(?:offer|provide|support|sponsor)\w*(?:\s+\w+){0,3}\s+(?:visa|sponsorship|work permit)",
    r"(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:not|unavailable|isn't)\s*(?:available|provided|offered|possible)?",
    r"\bno\s+(?:visa\s+)?sponsorship\b",
    r"without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship",
    r"must\s+(?:already\s+)?(?:have|hold|possess)\s+(?:a\s+|the\s+|full\s+|valid\s+|an?\s+existing\s+)*"
    r"(?:right|authori[sz]ation|permission|eligibility|permit)\s+to\s+work",
    r"(?:right|authori[sz]ation|eligib\w+)\s+to\s+work\s+in\s+[\w\s]{2,30}\s+(?:is\s+)?(?:required|mandatory|essential)",
    r"(?:only|exclusively)\s+(?:consider|accept)\w*\s+(?:candidates|applicants)\s+(?:who|with)\s+"
    r"(?:already\s+)?(?:have|hold)\s+(?:the\s+)?(?:right|permission|authori[sz]ation)",
    r"\b(?:eu|eea|uk|us)\s+(?:citizenship|passport)\s+(?:is\s+)?required",
    r"we\s+(?:are\s+)?(?:not|unable)\s+(?:currently\s+)?(?:able\s+to\s+)?sponsor",
]
POSITIVE = [
    r"(?:visa|work[- ]permit|immigration)\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered|possible)",
    r"we\s+(?:can|will|do|are able to|are happy to|offer to)\s+(?:also\s+)?(?:provide\s+)?sponsor",
    r"(?:offer|provide|including|includes|with)\s+(?:full\s+)?(?:visa|work[- ]permit|immigration)\s+"
    r"(?:sponsorship|support|assistance)",
    r"(?:visa|work[- ]permit|blue card)\s+(?:support|assistance|sponsorship)\s+(?:is\s+)?(?:available|provided|offered)",
    r"sponsorship\s+(?:for|of)\s+(?:your\s+)?(?:visa|work permit|blue card)",
    r"(?:support|help)\s+(?:you\s+)?(?:with|in obtaining|obtain)\s+(?:your\s+|a\s+|the\s+)?"
    r"(?:visa|work permit|blue card|residence permit)",
]
INTERNATIONAL_WELCOME = r"(?:international|non-eu|overseas|global)\s+(?:candidates|applicants|talent)\s+(?:are\s+)?welcome"
RELOCATION = r"relocation\s+(?:package|support|assistance|bonus|budget|allowance)"

NEG_RX = [re.compile(p, re.I) for p in NEGATIVE]
POS_RX = [re.compile(p, re.I) for p in POSITIVE]


def _snippet(text: str, m: re.Match) -> str:
    s = max(0, m.start() - 70)
    return re.sub(r"\s+", " ", text[s:m.end() + 70]).strip()


def classify(text: str, *, country: str | None, candidate: dict, employer: dict | None = None,
             source_url: str | None = None, checked_at: str | None = None) -> dict:
    checked_at = checked_at or iso()
    text = text or ""
    evidence = []
    needs = True
    if country and (country in (candidate.get("work_authorization") or {})):
        needs = False
    neg = [m for rx in NEG_RX for m in [rx.search(text)] if m]
    pos = [m for rx in POS_RX for m in [rx.search(text)] if m]
    # A positive regex can fire inside a negative sentence ("we cannot provide visa sponsorship").
    pos = [m for m in pos if not any(n.start() <= m.start() <= n.end() + 5 for n in neg)]
    for m in neg:
        evidence.append({"kind": "posting_negative", "text": _snippet(text, m), "source": source_url or "job posting",
                         "checked_at": checked_at})
    for m in pos:
        evidence.append({"kind": "posting_positive", "text": _snippet(text, m), "source": source_url or "job posting",
                         "checked_at": checked_at})
    intl = re.search(INTERNATIONAL_WELCOME, text, re.I)
    reloc = re.search(RELOCATION, text, re.I)
    if intl:
        evidence.append({"kind": "international_welcome", "text": _snippet(text, intl), "source": source_url or
                         "job posting", "checked_at": checked_at})
    if reloc:
        evidence.append({"kind": "relocation_support", "text": _snippet(text, reloc), "source": source_url or
                         "job posting", "checked_at": checked_at})
    reg = None
    if employer and country == "GB":
        status = employer.get("uk_sponsor_status")
        if status == "listed":
            reg = "listed"
            evidence.append({"kind": "uk_register_listed",
                             "text": f"Employer appears on the UK Home Office Register of Licensed Sponsors "
                                     f"({employer.get('uk_sponsor_routes') or 'route not recorded'}). This shows a "
                                     "licence, not that this role will be sponsored.",
                             "source": "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
                             "checked_at": employer.get("uk_sponsor_checked_at") or checked_at})
        elif status == "not_listed":
            reg = "not_listed"
            evidence.append({"kind": "uk_register_not_listed",
                             "text": "No exact match on the UK Register of Licensed Sponsors. A UK employer without a "
                                     "licence cannot sponsor a Skilled Worker visa (the legal entity name may differ).",
                             "source": "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
                             "checked_at": employer.get("uk_sponsor_checked_at") or checked_at})
        elif status and status.startswith("possible:"):
            evidence.append({"kind": "uk_register_possible",
                             "text": f"Possible register match '{status[9:]}' - verify the legal entity.",
                             "source": "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
                             "checked_at": employer.get("uk_sponsor_checked_at") or checked_at})

    if neg and pos:
        cls, note = "unknown", "Posting contains both positive and negative sponsorship statements; read it manually."
    elif neg:
        cls, note = "not_supported", "Posting states sponsorship is unavailable or existing work rights are required."
    elif pos:
        cls, note = "confirmed", "Posting explicitly offers visa / work-permit sponsorship or support."
    elif reg == "listed":
        cls, note = "likely", "Employer holds a UK sponsor licence; this specific role is not confirmed."
    elif intl and reloc:
        cls, note = "likely", "Posting welcomes international applicants and offers relocation support."
    else:
        cls, note = "unknown", "No sponsorship statement found in the posting."
    if not needs:
        note = "You already have the right to work here; sponsorship is not needed. " + note
    return {"classification": cls, "evidence": evidence, "note": note, "needs_sponsorship": needs}


# ---------------------------------------------------------------- UK licensed sponsor register

GOVUK_API = "https://www.gov.uk/api/content/government/publications/register-of-licensed-sponsors-workers"
REGISTER_KIND = "uk_sponsor_register"


def _register_age_days(db: Database) -> float | None:
    row = db.one("SELECT created_at FROM files WHERE kind=? ORDER BY id DESC LIMIT 1", (REGISTER_KIND,))
    if not row:
        return None
    dt = parse_iso(row["created_at"])
    return (utcnow() - dt).total_seconds() / 86400 if dt else None


def refresh_uk_register(db: Database, http: HttpClient, max_age_days: int = 7) -> dict:
    """Download the official register CSV (if stale) and store a compact gzip of normalised names."""
    age = _register_age_days(db)
    if age is not None and age < max_age_days:
        return {"refreshed": False, "age_days": round(age, 1)}
    meta = http.get_json(GOVUK_API)
    url = next((a.get("url") for a in (meta.get("details") or {}).get("attachments", [])
                if str(a.get("url", "")).lower().endswith(".csv")), None)
    if not url or not url.startswith("https://assets.publishing.service.gov.uk/"):
        raise FetchError("Could not locate the register CSV on GOV.UK")
    resp = http.get(url)
    text = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    names: dict[str, set] = {}
    for row in reader:
        org = row.get("Organisation Name") or row.get("Organisation name") or ""
        route = row.get("Route") or ""
        n = normalize_employer(org)
        if n and n != "unknown":
            names.setdefault(n, set()).add(route.strip())
    payload = "\n".join(f"{n}\t{'|'.join(sorted(r))}" for n, r in sorted(names.items()))
    blob = gzip.compress(payload.encode("utf-8"))
    with db.transaction():
        db.run("DELETE FROM files WHERE kind=?", (REGISTER_KIND,))
        db.insert("INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, created_at) "
                  "VALUES (?, ?, 'application/gzip', ?, '', ?, ?)",
                  (REGISTER_KIND, url.rsplit("/", 1)[-1], len(blob), db.blob(blob), iso()))
    _cache.clear()
    return {"refreshed": True, "organisations": len(names), "file": url.rsplit("/", 1)[-1]}


_cache: dict = {}


def load_register(db: Database) -> dict[str, str] | None:
    row = db.one("SELECT id, data FROM files WHERE kind=? ORDER BY id DESC LIMIT 1", (REGISTER_KIND,))
    if not row:
        return None
    if _cache.get("id") == row["id"]:
        return _cache["names"]
    names = {}
    for line in gzip.decompress(row["data"]).decode("utf-8").splitlines():
        n, _, routes = line.partition("\t")
        names[n] = routes
    _cache.update({"id": row["id"], "names": names})
    return names


def register_lookup(names: dict[str, str], employer_norm: str) -> tuple[str, str | None]:
    """('listed', routes) for exact normalised match; ('possible:<name>', routes) for a prefix match on a
    distinctive name; ('not_listed', None) otherwise."""
    if employer_norm in names:
        return "listed", names[employer_norm]
    if len(employer_norm) >= 5:
        for n in names:
            if n.startswith(employer_norm + " ") and len(n.split()) <= len(employer_norm.split()) + 2:
                return f"possible:{n}", names[n]
    return "not_listed", None


COUNTRY_GUIDANCE = {
    "IE": ("Critical Skills / General Employment Permit (DETE)",
           "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
           ["Is the role on the Critical Skills Occupations List?", "Does the offer meet the current permit salary "
            "threshold?", "Will the employer file the permit application and pay the fee?"]),
    "GB": ("Skilled Worker visa (Home Office)", "https://www.gov.uk/skilled-worker-visa",
           ["Will the employer assign a Certificate of Sponsorship for this role?", "Does the salary meet the "
            "current Skilled Worker threshold for the occupation code?", "Who pays the Immigration Skills Charge "
            "and visa fees?"]),
    "DE": ("EU Blue Card / skilled worker residence permit", "https://www.make-it-in-germany.com/en/visa-residence/types/eu-blue-card",
           ["Is your degree recognised (anabin / ZAB statement)?", "Does the offer meet the current Blue Card "
            "salary threshold?", "Is German required for the team?"]),
    "NL": ("Highly skilled migrant (IND recognised sponsor)", "https://ind.nl/en/public-register-recognised-sponsors",
           ["Is the employer on the IND public register of recognised sponsors?", "Does the salary meet the "
            "current highly-skilled-migrant criterion for your age?", "Is the 30% ruling offered?"]),
    "SE": ("Work permit (Migrationsverket)", "https://www.migrationsverket.se/English/Private-individuals/Working-in-Sweden.html",
           ["Does the salary meet the current work-permit maintenance requirement?", "Is the employer certified "
            "for the fast-track process?"]),
    "DK": ("Pay Limit / Fast-track scheme (SIRI)", "https://www.nyidanmark.dk/en-GB/You-want-to-apply/Work",
           ["Is the employer certified for the fast-track scheme?", "Does the salary meet the pay-limit scheme?"]),
    "FI": ("Residence permit for specialists (Migri)", "https://migri.fi/en/specialist",
           ["Is the fast-track (14-day) service available for this role?"]),
    "AT": ("Red-White-Red Card / EU Blue Card", "https://www.migration.gv.at/en/",
           ["Which Red-White-Red category applies and do you meet the points?"]),
    "CH": ("Non-EU work permit (SEM, quota-based)", "https://www.sem.admin.ch/sem/en/home/themen/arbeit/nicht-eu_efta-angehoerige.html",
           ["Has the employer shown no suitable Swiss/EU candidate was found?", "Is a quota available this year?"]),
    "BE": ("Single permit", "https://www.belgium.be/en/work/coming_to_work_in_belgium",
           ["Which region (Flanders/Brussels/Wallonia) processes the permit?"]),
    "LU": ("EU Blue Card / salaried worker permit", "https://guichet.public.lu/en/citoyens/immigration.html",
           ["Has the vacancy been declared to ADEM?"]),
    "PL": ("Work permit + temporary residence", "https://www.gov.pl/web/udsc-en", []),
    "PT": ("Highly qualified activity / tech visa", "https://aima.gov.pt/", []),
    "ES": ("Highly qualified professional visa", "https://www.exteriores.gob.es/", []),
    "IT": ("EU Blue Card (quota-exempt)", "https://integrazionemigranti.gov.it/", []),
    "CZ": ("Employee card / EU Blue Card", "https://ipc.gov.cz/en/", []),
    "EE": ("D-visa / temporary residence for work", "https://www.workinestonia.com/", []),
    "CA": ("Work permit via LMIA / Global Talent Stream, or Express Entry (IRCC)",
           "https://www.canada.ca/en/immigration-refugees-citizenship/services/work-canada.html",
           ["Will the employer obtain an LMIA or use the Global Talent Stream?", "Is the role LMIA-exempt (e.g. "
            "intra-company transfer)?", "Would they support Express Entry (job offer points)?"]),
    "AU": ("Skills in Demand (subclass 482) employer-sponsored visa (Home Affairs)",
           "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/skills-in-demand-visa-subclass-482",
           ["Is the employer an approved standard business sponsor?", "Does the salary meet the current income "
            "threshold for the stream?", "Is a skills assessment needed for the occupation?"]),
    "AE": ("Employment visa + residence permit (employer-sponsored, MOHRE / ICP)",
           "https://u.ae/en/information-and-services/jobs/employment-in-the-private-sector",
           ["Does the employer sponsor the employment visa, medical and Emirates ID costs?", "Is the offer "
            "letter registered with MOHRE (or the free-zone authority)?", "Is housing / flight allowance included?"]),
    "JP": ("Engineer / Specialist in Humanities / International Services visa (Certificate of Eligibility)",
           "https://www.moj.go.jp/isa/applications/status/gijinkoku.html",
           ["Will the employer apply for your Certificate of Eligibility (CoE)?", "Is Japanese required for the "
            "team or only for daily life?", "Does the role qualify for Highly Skilled Professional points?"]),
}
