"""Deterministic cover letters built only from resume sentences and confirmed facts (AI version is optional)."""
from __future__ import annotations

from .settings import COUNTRY_NAMES
from .skills import find_skills


def resume_bullets(structured: dict) -> list[str]:
    out = []
    for role in structured.get("experience", []):
        out += [b for b in role.get("bullets", []) if "|" not in b]
        for p in role.get("projects", []):
            out += p.get("bullets", [])
    for p in structured.get("projects", []):
        out += p.get("bullets", [])
    return out


def relevant_bullets(structured: dict, skills: list[str], n: int = 3) -> list[str]:
    wanted = set(skills)
    scored = []
    for i, b in enumerate(resume_bullets(structured)):
        hits = set(find_skills(b)) & wanted
        scored.append((len(hits), any(ch.isdigit() for ch in b), -i, b))
    scored.sort(reverse=True)
    return [b for *_, b in scored[:n]]


def facts_for_letter(structured: dict, candidate: dict, profile: dict, job: dict) -> dict:
    """The only information an AI writer is allowed to use."""
    country = job.get("country")
    return {
        "name": candidate.get("full_name"),
        "current_role": (structured.get("experience") or [{}])[0].get("title"),
        "current_employer": (structured.get("experience") or [{}])[0].get("company"),
        "experience_years": profile.get("experience_years"),
        "skills_demonstrated": sorted(k for k, v in profile["skills"].items() if v == "demonstrated"),
        "resume_bullets": resume_bullets(structured)[:14],
        "education": structured.get("education"),
        "awards": structured.get("achievements"),
        "requires_sponsorship": bool(country and country not in (candidate.get("work_authorization") or {})
                                     and candidate.get("requires_sponsorship_outside")),
        "nationality": candidate.get("nationality"),
        "location": candidate.get("current_location"),
        "target_country": COUNTRY_NAMES.get(country) if country else None,
    }


def template_letter(structured: dict, candidate: dict, profile: dict, job: dict, employer: str,
                    must_have: list[str]) -> str:
    facts = facts_for_letter(structured, candidate, profile, job)
    matched = [s for s in must_have if profile["skills"].get(s) == "demonstrated"][:4]
    bullets = relevant_bullets(structured, must_have or matched, 3)
    years = int(facts["experience_years"] or 0)
    lines = [f"Dear {employer} Hiring Team,", ""]
    where = f" in {job['city']}" if job.get("city") else (f" in {facts['target_country']}" if facts["target_country"] else "")
    intro = f"I am applying for the {job['title']} role{where}. I am a {facts['current_role'] or 'software engineer'}"
    if facts["current_employer"]:
        intro += f" at {facts['current_employer']}"
    if years:
        intro += f" with {years}+ years of professional experience"
    if matched:
        intro += f", working day to day with {', '.join(matched[:-1]) + ' and ' + matched[-1] if len(matched) > 1 else matched[0]}"
    lines += [intro + ".", ""]
    if bullets:
        lines.append("A few examples relevant to this role:")
        lines += [f"• {b}" for b in bullets]
        lines.append("")
    if facts["requires_sponsorship"]:
        lines += [f"For transparency: I am an {facts['nationality']} national based in {facts['location']} and would "
                  f"require visa sponsorship to work in {facts['target_country']}. I am ready to relocate.", ""]
    lines += [f"I would welcome the opportunity to discuss how I can contribute to {employer}.", "", "Kind regards,",
              candidate.get("full_name") or "",
              " | ".join(x for x in [candidate.get("email"), candidate.get("phone"), candidate.get("linkedin_url")] if x)]
    return "\n".join(lines)
