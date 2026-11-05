"""Deterministic job-description analysis. Job text is untrusted data: it is only pattern-matched,
never executed or followed as instructions."""
from __future__ import annotations

import re

from .skills import GENERIC, find_skills

MUST_HEADERS = re.compile(
    r"(requirements|what you('ll)? (need|bring)|you (have|bring|are)|must[- ]have|qualifications|"
    r"what we('re)? looking for|about you|your profile|who you are|minimum qualifications|essential|"
    r"required skills|skills (and|&) experience|your skills|ihr profil|was du mitbringst)", re.I)
NICE_HEADERS = re.compile(
    r"(nice[- ]to[- ]have|bonus|preferred|plus(es)?|desirable|would be great|ideally|extra credit|"
    r"good to have|it'?s a plus|pluspunkte|wünschenswert)", re.I)
OTHER_HEADERS = re.compile(
    r"(what we offer|benefits|perks|about us|who we are|our team|why join|compensation|salary|"
    r"responsibilities|what you('ll)? do|the role|your role|day to day|your mission|about the role|"
    r"equal opportunity|diversity|how to apply|interview process)", re.I)

LANG_NAMES = ["german", "dutch", "french", "swedish", "danish", "finnish", "norwegian", "italian", "spanish",
              "portuguese", "polish", "czech", "estonian", "english", "flemish", "luxembourgish"]
CEFR = r"(a1|a2|b1|b2|c1|c2)"

EMPLOYMENT_TYPES = [
    ("internship", r"\b(intern(ship)?|praktikum|working student|werkstudent|trainee)\b"),
    ("contract", r"\b(contract(or)?|freelance|fixed[- ]term|temporary contract|b2b)\b"),
    ("temporary", r"\b(temporary|temp|seasonal|befristet)\b"),
    ("part_time", r"\b(part[- ]time|teilzeit)\b"),
    ("full_time", r"\b(full[- ]time|permanent|vollzeit|festanstellung|regular)\b"),
]


def employment_type(raw: str | None, title: str, text: str) -> str:
    for kind, pat in EMPLOYMENT_TYPES:
        if raw and re.search(pat, raw, re.I):
            return kind
    for kind, pat in EMPLOYMENT_TYPES[:2]:
        if re.search(pat, title, re.I):
            return kind
    head = text[:1500]
    for kind, pat in EMPLOYMENT_TYPES:
        if re.search(pat, head, re.I):
            return kind
    return "full_time" if raw is None else "unspecified"


def split_sections(text: str) -> dict[str, str]:
    """Classify paragraphs/lines into must / nice / other / intro buckets by the nearest preceding header."""
    buckets = {"must": [], "nice": [], "other": [], "intro": []}
    current = "intro"
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        is_header = len(s) < 90 and not s.startswith("•")
        if is_header and NICE_HEADERS.search(s):
            current = "nice"
        elif is_header and MUST_HEADERS.search(s):
            current = "must"
        elif is_header and OTHER_HEADERS.search(s):
            current = "other"
        # Inline markers override bucket for this line.
        if re.search(r"\b(nice to have|is a plus|a plus|bonus|preferred|ideally)\b", s, re.I):
            buckets["nice"].append(s)
        else:
            buckets[current].append(s)
    return {k: "\n".join(v) for k, v in buckets.items()}


def experience_years(text: str) -> tuple[float | None, str | None]:
    """Smallest explicit 'N+ years' requirement found, with the evidence sentence."""
    best, snippet = None, None
    for m in re.finditer(
            r"(?:(?:at least|minimum(?: of)?|min\.?|over)\s+)?(\d{1,2})(?:\s*(?:-|–|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?|jahre)"
            r"(?:\s+of)?(?:\s+\w+){0,6}?\s+(?:experience|exp\b|erfahrung|in (?:software|backend|development|engineering))",
            text or "", re.I):
        n = float(m.group(1))
        if n > 25:
            continue
        if best is None or n < best:
            best = n
            start = max(0, m.start() - 60)
            snippet = text[start:m.end() + 40].replace("\n", " ").strip()
    return best, snippet


def language_requirements(text: str) -> list[dict]:
    """Detect spoken-language requirements other than programming languages."""
    out = []
    low = text or ""
    for lang in LANG_NAMES:
        for m in re.finditer(r"(?<![a-z])" + lang + r"(?![a-z])", low, re.I):
            window = low[max(0, m.start() - 90): m.end() + 90]
            if not re.search(r"speak|spoken|fluen|proficien|native|language|written|verbal|level|"
                             + CEFR + r"|business|conversational|working knowledge|sprach|kenntnisse|deutsch",
                             window, re.I):
                continue
            level_m = re.search(CEFR, window, re.I)
            nice = bool(re.search(r"plus|nice|bonus|advantage|preferred|beneficial|desirable|ideally|helpful|"
                                  r"not required|no .{0,20} required|wünschenswert", window, re.I))
            required = not nice and bool(re.search(r"fluen|required|must|essential|native|business|proficien|"
                                                   r"excellent|very good|strong|sehr gut|verhandlungssicher|"
                                                   r"fließend|c1|c2|b2", window, re.I))
            out.append({"language": lang.title(), "level": level_m.group(1).upper() if level_m else None,
                        "required": required, "evidence": window.replace("\n", " ").strip()})
            break
    # German-language postings implicitly require German
    if re.search(r"\b(wir suchen|deine aufgaben|ihre aufgaben|dein profil|ihr profil|was wir bieten)\b", low, re.I) \
            and not any(o["language"] == "German" for o in out):
        out.append({"language": "German", "level": None, "required": True,
                    "evidence": "Job description is written in German."})
    return out


def salary_from_text(text: str) -> dict | None:
    m = re.search(r"(€|eur|£|gbp|chf|sek|dkk|pln|\$|usd)\s?(\d{2,3})[.,]?(\d{3})?\s*(k)?\s*(?:-|–|to)\s*(€|eur|£|gbp|chf|"
                  r"sek|dkk|pln|\$|usd)?\s?(\d{2,3})[.,]?(\d{3})?\s*(k)?", text or "", re.I)
    if not m:
        return None
    cur_map = {"€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "chf": "CHF", "sek": "SEK", "dkk": "DKK",
               "pln": "PLN", "$": "USD", "usd": "USD"}

    def val(a, b, k):
        v = float(a + (b or ""))
        if k or (not b and v < 1000):
            v *= 1000
        return v
    lo, hi = val(m.group(2), m.group(3), m.group(4)), val(m.group(6), m.group(7), m.group(8))
    if lo < 10000 or hi < lo or hi > 1_000_000:
        return None
    return {"min": lo, "max": hi, "currency": cur_map[m.group(1).lower()], "period": "year",
            "evidence": m.group(0)}


def remote_type(text: str, location: dict, hint: str | None) -> str:
    if hint in ("remote", "hybrid", "onsite"):
        return hint
    if location.get("remote_type") != "unknown":
        return location["remote_type"]
    head = (text or "")[:3000]
    if re.search(r"\bfully remote\b|\b100% remote\b|\bremote[- ]first\b", head, re.I):
        return "remote"
    if re.search(r"\bhybrid\b|\d days? (a|per) week in (the )?office", head, re.I):
        return "hybrid"
    if re.search(r"\bon-?site\b|\bin[- ]office\b|\boffice[- ]based\b", head, re.I):
        return "onsite"
    return "unknown"


def remote_scope(text: str, location_raw: str | None) -> str | None:
    blob = f"{location_raw or ''}\n{text or ''}"
    if re.search(r"\b(india|apac|asia)\b.{0,40}\b(remote|eligible|welcome)|remote.{0,40}\b(india|apac)\b", blob, re.I):
        return "india_allowed"
    if re.search(r"\b(worldwide|anywhere in the world|work from anywhere|globally)\b", blob, re.I):
        return "worldwide"
    if re.search(r"\bremote\b.{0,40}\b(eu|europe|emea|eea)\b|\b(eu|europe|emea|eea)\b.{0,20}\bremote\b", blob, re.I):
        return "europe_only"
    if re.search(r"\bremote\b.{0,30}\b(us|usa|united states|canada|north america)\b", blob, re.I):
        return "americas_only"
    return None


def education(text: str) -> str | None:
    m = re.search(r"[^.\n]{0,80}\b(bachelor'?s?|master'?s?|degree|b\.?sc|m\.?sc|phd|computer science)\b[^.\n]{0,80}",
                  text or "", re.I)
    return m.group(0).strip() if m else None


def relocation(text: str) -> str | None:
    m = re.search(r"[^.\n]{0,100}\breloca\w*[^.\n]{0,120}", text or "", re.I)
    return m.group(0).strip() if m else None


def analyze(title: str, text: str) -> dict:
    sections = split_sections(text)
    must_text = sections["must"] or sections["intro"] + "\n" + sections["other"]
    must = find_skills(must_text)
    nice = find_skills(sections["nice"])
    title_skills = find_skills(title)
    for sk in title_skills:  # skills in the title are always must-haves
        must.setdefault(sk, title_skills[sk])
    for sk in list(nice):
        if sk in must:
            nice.pop(sk)
    years, yrs_evidence = experience_years(text)
    return {
        "must_have": sorted(must),
        "nice_to_have": sorted(nice),
        "all_skills": sorted(set(find_skills(text)) | set(title_skills)),
        "generic": sorted(s for s in must if s in GENERIC),
        "experience_years": years,
        "experience_evidence": yrs_evidence,
        "languages": language_requirements(text),
        "education": education(text),
        "relocation": relocation(text),
        "salary": salary_from_text(text),
        "responsibilities": [l.lstrip("• ").strip() for l in sections["other"].splitlines()
                             if l.strip().startswith("•")][:8],
    }
