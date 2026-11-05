"""Deterministic resume quality analysis: impact, wording, ATS problems, inconsistencies, claim checks."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from ..skills import IMPLIES, find_skills
from .parser import experience_months, parse_month_year

WEAK_STARTS = {
    "worked", "helped", "responsible", "assisted", "handled", "involved", "participated", "did", "made",
    "used", "tasked", "duties", "supported", "contributed",
}
STRONG_VERBS = [
    "Built", "Designed", "Engineered", "Implemented", "Automated", "Optimised", "Delivered", "Led",
    "Migrated", "Integrated", "Reduced", "Shipped", "Launched", "Refactored", "Developed", "Architected",
]
RISKY_CLAIMS = [
    (r"eligible for (?:the )?eu blue card", "Blue Card eligibility depends on the specific offer (salary threshold) "
     "and degree recognition. Say 'requires visa sponsorship' instead of asserting eligibility."),
    (r"\bfluent in\b", "Make sure language fluency claims match a level you can demonstrate (e.g. CEFR level)."),
    (r"\bexpert\b", "'Expert' invites hard questions; prefer concrete evidence."),
    (r"vibe coding", "Informal term; recruiters may read it as low rigour. Consider 'AI-assisted development "
     "with code review and tests'."),
]


def _all_bullets(s: dict) -> list[tuple[str, str]]:
    out = []
    for role in s.get("experience", []):
        for b in role.get("bullets", []):
            out.append((f"{role.get('title')} @ {role.get('company')}", b))
        for proj in role.get("projects", []):
            for b in proj.get("bullets", []):
                out.append((proj.get("name") or "project", b))
    for proj in s.get("projects", []):
        for b in proj.get("bullets", []):
            out.append((proj.get("name") or "project", b))
    return out


def skill_evidence(s: dict) -> dict[str, str]:
    """For each detected skill: 'demonstrated' (in experience/project bullets or tech tags) or 'listed'."""
    evidence_text = []
    for role in s.get("experience", []):
        evidence_text += role.get("bullets", [])
        for proj in role.get("projects", []):
            evidence_text += proj.get("bullets", []) + proj.get("tech", [])
    for proj in s.get("projects", []):
        evidence_text += proj.get("bullets", []) + proj.get("tech", [])
    demonstrated = set(find_skills(" \n ".join(evidence_text)))
    for sk in list(demonstrated):
        demonstrated |= IMPLIES.get(sk, set())
    listed_text = " \n ".join(item for items in s.get("skills", {}).values() for item in items)
    listed = set(find_skills(listed_text)) | set(find_skills(s.get("summary", "")))
    result = {sk: "demonstrated" for sk in demonstrated}
    for sk in listed - demonstrated:
        result[sk] = "listed"
    return dict(sorted(result.items()))


def analyze(s: dict, candidate: dict | None = None, target_countries: list[str] | None = None,
            today: date | None = None) -> dict:
    today = today or date.today()
    candidate = candidate or {}
    issues: list[dict] = []

    def add(severity, category, message, location=None, suggestion=None):
        issues.append({"severity": severity, "category": category, "message": message,
                       "location": location, "suggestion": suggestion})

    bullets = _all_bullets(s)
    if not bullets:
        add("high", "content", "No experience or project bullet points were found.")

    # --- impact & wording
    with_metric = 0
    first_words = Counter()
    for loc, b in bullets:
        words = b.split()
        if not words:
            continue
        fw = re.sub(r"[^A-Za-z]", "", words[0]).lower()
        first_words[fw] += 1
        if re.search(r"\d", b):
            with_metric += 1
        else:
            add("low", "impact", f"No measurable result: \"{b[:90]}...\"" if len(b) > 90 else
                f"No measurable result: \"{b}\"", loc,
                "Add a number you can defend (time saved, users served, % improvement, volume) if one exists. "
                "Do not invent metrics.")
        if fw in WEAK_STARTS:
            add("medium", "wording", f"Weak opening verb '{words[0]}'.", loc,
                f"Start with a strong action verb, e.g. {', '.join(STRONG_VERBS[:5])}.")
        if len(b) > 260:
            add("low", "wording", "Very long bullet (over 260 characters); recruiters skim.", loc,
                "Split into two bullets or cut detail.")
    for verb, n in first_words.items():
        if n >= 3 and verb:
            add("low", "wording", f"{n} bullets start with '{verb.capitalize()}'.", None,
                "Vary opening verbs so each bullet reads as a distinct achievement.")
    metric_ratio = (with_metric / len(bullets)) if bullets else 0.0

    # --- ATS / layout
    heads = s.get("raw_headings", {})
    for key, raw in heads.items():
        if re.search(r"\b[A-Z] [A-Z]{2,}", raw):
            add("high", "ats", f"Heading '{raw}' contains a broken letter-spaced word; ATS may not recognise the "
                f"'{key}' section.", raw, "Use a plain heading such as 'EXPERIENCE' without letter-spacing.")
    layout = s.get("layout", {})
    if layout.get("two_column"):
        add("medium", "ats", "Two-column region (e.g. Education beside Certifications). Many ATS read straight "
            "across, merging both columns.", None, "Use a single-column layout for ATS submissions.")
    glyphs = [g for g in layout.get("bullet_glyphs", []) if g not in ("•", "-")]
    if glyphs:
        add("low", "ats", f"Decorative bullet glyphs {' '.join(glyphs)} are extracted as separate characters by "
            "some parsers.", None, "Use standard round bullets in ATS versions.")
    if "achievements" in heads and "certification" in heads["achievements"].lower():
        achievements = s.get("achievements", [])
        real_certs = [a for a in achievements if re.search(r"certified|certification\b|AZ-\d|AWS Certified", a, re.I)]
        if achievements and not real_certs:
            add("low", "ats", "Section titled 'Certifications' holds awards/recognitions, not formal "
                "certifications. Recruiters searching for certifications may be misled.",
                heads["achievements"], "Rename to 'Awards & Recognition'.")
    if not s.get("contact", {}).get("email"):
        add("high", "content", "No email address found in the header.")
    if not s.get("contact", {}).get("linkedin"):
        add("low", "content", "No LinkedIn URL in the header.")

    # --- profile consistency with confirmed candidate facts
    linkedin_resume = (s.get("contact", {}).get("linkedin") or "").lower().rstrip("/")
    linkedin_conf = (candidate.get("linkedin_url") or "").lower().rstrip("/")
    if linkedin_resume and linkedin_conf and not linkedin_conf.endswith(linkedin_resume.split("linkedin.com")[-1]):
        add("high", "consistency", f"LinkedIn URL on the resume ({linkedin_resume}) differs from the profile URL "
            f"you provided ({linkedin_conf}). Recruiters may land on the wrong page.", "header",
            "Use the same, correct LinkedIn URL everywhere.")
    tagline = (s.get("tagline") or "") + " " + s.get("summary", "")
    overseas = [c for c in (target_countries or []) if c != "IN"]
    if overseas and re.search(r"anywhere in india|in india\b", tagline, re.I):
        add("high", "targeting", "Header/summary says you are seeking roles in India, but your targets are "
            "overseas. Use a country-specific variant.", "header",
            "Generate a variant for the target country; state 'Requires visa sponsorship' plainly.")
    if overseas and candidate.get("requires_sponsorship_outside") and not re.search(r"sponsor", tagline, re.I):
        add("medium", "targeting", "Overseas applications should state your work-authorization status clearly "
            "to avoid wasted screening rounds.", "header", "Add 'Indian national · Requires visa sponsorship'.")

    # --- inconsistencies in dates and claims
    months = experience_months(s, today)
    claimed = re.search(r"(\d+)\+?\s*years", s.get("summary", ""), re.I)
    if claimed and months:
        claimed_years = int(claimed.group(1))
        if months < claimed_years * 12:
            add("high", "consistency", f"Summary claims {claimed_years}+ years but dated experience totals "
                f"{months} months.", "summary", "Make the summary match your dated experience.")
    for role in s.get("experience", []):
        start = parse_month_year(role.get("start") or "")
        end_raw = (role.get("end") or "").lower()
        end = today if end_raw in ("present", "current", "now") else parse_month_year(end_raw)
        if start is None:
            add("medium", "consistency", f"Role '{role.get('title')}' has no parseable start date.")
        elif start > today:
            add("high", "consistency", f"Role '{role.get('title')}' starts in the future ({role.get('start')}).")
        elif end and end < start:
            add("high", "consistency", f"Role '{role.get('title')}' ends before it starts.")
    for edu in s.get("education", []):
        if edu.get("start_year") and edu.get("end_year") and edu["end_year"].isdigit() \
                and int(edu["end_year"]) < int(edu["start_year"]):
            add("high", "consistency", "Education end year is before its start year.")
    for proj in s.get("projects", []):
        d = parse_month_year(proj.get("date") or "")
        if d and d > today:
            add("medium", "consistency", f"Project '{proj['name']}' is dated in the future ({proj['date']}).")

    full_text = s.get("text", "")
    for pattern, advice in RISKY_CLAIMS:
        m = re.search(pattern, full_text, re.I)
        if m:
            add("medium", "claims", f"Phrase '{m.group(0)}' needs care.", None, advice)

    # --- skills
    all_listed = [i.strip().lower() for items in s.get("skills", {}).values() for i in items]
    for item, n in Counter(all_listed).items():
        if n > 1:
            add("low", "skills", f"Skill '{item}' is listed {n} times.")
    evidence = skill_evidence(s)
    listed_only = [k for k, v in evidence.items() if v == "listed"]
    if listed_only:
        add("medium", "skills", "Listed in skills but not shown in any experience/project bullet: "
            + ", ".join(listed_only) + ". Interviewers will probe these.", "skills",
            "Either add a truthful bullet showing where you used each, or be ready to discuss it honestly.")

    sev_weight = {"high": 10, "medium": 4, "low": 1}
    score = max(0, round(100 - sum(sev_weight[i["severity"]] for i in issues)))
    return {
        "score": score,
        "experience_months": months,
        "bullet_count": len(bullets),
        "metric_ratio": round(metric_ratio, 2),
        "skill_evidence": evidence,
        "issues": sorted(issues, key=lambda i: {"high": 0, "medium": 1, "low": 2}[i["severity"]]),
        "needs_review": [i for i in issues if i["category"] in ("consistency", "claims", "targeting")],
    }
