"""Generate role- and country-specific resume variants WITHOUT changing facts.

Allowed changes: reordering sections/skills/bullets, ATS-safe headings, header targeting line built from
user-confirmed facts, and a summary assembled only from sentences/facts already in the canonical resume.
Every change is recorded as {section, before, after, reason}."""
from __future__ import annotations

import copy
import re

from ..settings import COUNTRY_NAMES
from ..skills import find_skills
from .parser import experience_months
from .styles import role_families, to_uk, vocabulary

# Role families now live in styles.json (data, not code). Kept as a module attribute for backwards compatibility.
ROLE_FAMILIES = role_families()

ATS_HEADINGS = {
    "summary": "SUMMARY", "skills": "TECHNICAL SKILLS", "experience": "EXPERIENCE", "projects": "PROJECTS",
    "education": "EDUCATION", "achievements": "AWARDS & RECOGNITION",
}


def _relevance(text: str, priority: list[str]) -> int:
    found = find_skills(text)
    return sum(len(priority) - priority.index(s) for s in found if s in priority)


def build_variant(canonical: dict, role_family: str, country: str | None, candidate: dict) -> tuple[dict, list[dict]]:
    if role_family not in ROLE_FAMILIES:
        raise ValueError(f"Unknown role family '{role_family}'")
    fam = ROLE_FAMILIES[role_family]
    v = copy.deepcopy(canonical)
    changes: list[dict] = []

    # 1. ATS-safe headings and single column
    v["headings"] = dict(ATS_HEADINGS)
    for key, raw in canonical.get("raw_headings", {}).items():
        if raw != ATS_HEADINGS.get(key):
            changes.append({"section": "headings", "before": raw, "after": ATS_HEADINGS.get(key, raw),
                            "reason": "Standard heading that ATS parsers recognise."})
    if canonical.get("layout", {}).get("two_column"):
        changes.append({"section": "layout", "before": "Two-column bottom section", "after": "Single column",
                        "reason": "ATS parsers often read across columns and merge them."})
    if canonical.get("layout", {}).get("bullet_glyphs"):
        changes.append({"section": "layout", "before": " ".join(canonical["layout"]["bullet_glyphs"]),
                        "after": "• (standard bullet)", "reason": "Decorative glyphs extract as stray characters."})

    # 2. Header targeting line from confirmed facts only
    old_tag = canonical.get("tagline") or ""
    parts = []
    if candidate.get("current_location"):
        parts.append(f"Based in {candidate['current_location']}")
    if country and country != "IN":
        if candidate.get("nationality"):
            parts.append(f"{candidate['nationality']} national")
        wa = candidate.get("work_authorization", {})
        if country in wa:
            parts.append(f"Authorised to work in {COUNTRY_NAMES.get(country, country)}")
        elif candidate.get("requires_sponsorship_outside"):
            parts.append("Requires visa sponsorship")
        if candidate.get("willing_to_relocate"):
            parts.append(f"Open to relocation to {COUNTRY_NAMES.get(country, country)}")
    elif candidate.get("willing_to_relocate"):
        parts.append("Open to relocation")
    if "immediately available" in old_tag.lower():
        parts.append("Immediately Available")
    v["tagline"] = " · ".join(parts)
    if v["tagline"] != old_tag:
        changes.append({"section": "header", "before": old_tag, "after": v["tagline"],
                        "reason": "States location, work-authorization status and relocation for this target "
                                  "market, using facts you confirmed in Settings."})

    # 3. Correct LinkedIn URL if the user confirmed a different one
    conf_li = (candidate.get("linkedin_url") or "").replace("https://", "").replace("www.", "").rstrip("/")
    if conf_li and v.get("contact", {}).get("linkedin") and v["contact"]["linkedin"].rstrip("/") != conf_li:
        changes.append({"section": "header", "before": v["contact"]["linkedin"], "after": conf_li,
                        "reason": "Matches the LinkedIn profile URL you confirmed."})
        v["contact"]["linkedin"] = conf_li

    # 4. Summary: re-targeted, built only from existing summary sentences
    old_summary = canonical.get("summary", "")
    sentences = re.split(r"(?<=[.!?])\s+", old_summary.strip()) if old_summary else []
    keep = [s for s in sentences if not re.search(r"\bseeking\b", s, re.I)]
    months = experience_months(canonical)
    if keep:
        first = keep[0]
        years = months // 12
        if years >= 1:
            first = re.sub(r"^[^,]*?(?= with \d+\+? years)", fam["headline"], first, count=1) \
                if re.search(r" with \d+\+? years", first) else first
        keep[0] = first
    target = COUNTRY_NAMES.get(country, "") if country and country != "IN" else ""
    seeking = f"Seeking a {fam['headline']} role" + (f" in {target}" if target else "") + "."
    v["summary"] = " ".join(keep + [seeking]) if keep else old_summary
    if v["summary"] != old_summary:
        changes.append({"section": "summary", "before": old_summary, "after": v["summary"],
                        "reason": f"Retargeted to {fam['label']}" + (f" in {target}" if target else "")
                                  + "; India-specific job-search sentence removed. No new claims added."})

    # 5. Reorder skill categories and items by relevance (nothing added or removed)
    skills = canonical.get("skills", {})
    if skills:
        cats = sorted(skills.items(), key=lambda kv: -_relevance(" ".join(kv[1]) + " " + kv[0], fam["priority"]))
        new_skills = {}
        for cat, items in cats:
            new_skills[cat] = sorted(items, key=lambda it: -_relevance(it, fam["priority"]))
        if list(new_skills.items()) != list(skills.items()):
            changes.append({"section": "skills", "before": ", ".join(skills.keys()),
                            "after": ", ".join(new_skills.keys()),
                            "reason": "Most relevant skills for this role family first. Nothing added or removed."})
        v["skills"] = new_skills

    # 6. Reorder bullets within each project by relevance
    for role in v.get("experience", []):
        for proj in role.get("projects", []):
            before = list(proj["bullets"])
            proj["bullets"] = sorted(proj["bullets"], key=lambda b: -_relevance(b, fam["priority"]))
            if proj["bullets"] != before:
                changes.append({"section": f"experience/{proj['name']}", "before": before[0],
                                "after": proj["bullets"][0],
                                "reason": "Most relevant achievement for this role family moved to the top."})
        role["projects"] = sorted(role.get("projects", []),
                                  key=lambda p: -_relevance(" ".join(p["tech"] + p["bullets"]), fam["priority"]))
    # 7. Project section ordering
    v["projects"] = sorted(v.get("projects", []),
                           key=lambda p: -_relevance(" ".join(p.get("tech", []) + p.get("bullets", [])),
                                                     fam["priority"]))
    v["role_family"] = role_family
    v["target_country"] = country
    return v, changes


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _candidate_text(candidate: dict | None) -> str:
    c = candidate or {}
    parts = [c.get(k) for k in ("full_name", "email", "phone", "linkedin_url", "current_location", "nationality")]
    li = c.get("linkedin_url") or ""
    parts.append(li.replace("https://", "").replace("www.", "").rstrip("/"))
    return " ".join(str(p) for p in parts if p)


def source_words(text: str) -> set[str]:
    """Lower-cased, UK-spelling-normalised word set used for the proper-noun check."""
    return {to_uk(w.lower()) for w in _WORD.findall(text or "")}


def proper_noun_violations(out_text: str, src_words: set[str], extra: set[str] | frozenset[str] = frozenset()
                           ) -> list[str]:
    """Capitalised words that appear neither in the source nor in the allowlist. Sentence/line-initial words are
    also accepted when they look like a past-tense action verb (e.g. 'Streamlined'), since bullets start with one."""
    allowed = set(src_words) | set(extra) | set(vocabulary())
    problems = []
    for line in (out_text or "").splitlines():
        line = re.sub(r"^[\s•\-*]+", "", line)
        for sent in re.split(r"(?<=[.!?:;])\s+|\s+[·|]\s+", line):
            for i, m in enumerate(_WORD.finditer(sent)):
                w = m.group(0)
                if not w[0].isupper():
                    continue
                lw = to_uk(w.lower())
                if lw in allowed:
                    continue
                if i == 0 and len(w) > 3 and w[1:].islower() and lw.endswith("ed"):
                    continue
                problems.append(f"New name or proper noun '{w}' not present in the canonical resume")
    return sorted(set(problems))


def verify_no_new_facts(canonical: dict, variant: dict, candidate: dict | None = None, allowed_text: str = "",
                        check_proper_nouns: bool = False) -> list[str]:
    """Guard: every number and every recognised skill in the variant must already exist in the canonical resume,
    the user-confirmed candidate facts (Settings) or `allowed_text` (e.g. reformatted dates). With
    check_proper_nouns, capitalised words must also come from those sources or the styles.json vocabulary
    (headings, header phrases, neutral wording replacements). Returns a list of violations."""
    src = canonical.get("text", "") + " " + " ".join(
        str(x) for x in (canonical.get("contact") or {}).values() if x) + " " + _candidate_text(candidate) + " "         + (allowed_text or "")
    out_text = render_text(variant)
    problems = []
    src_numbers = set(_NUM.findall(src))
    for n in sorted(set(_NUM.findall(out_text)) - src_numbers):
        problems.append(f"New number '{n}' not present in the canonical resume")
    src_skills = set(find_skills(src))
    for sk in sorted(set(find_skills(out_text)) - src_skills):
        problems.append(f"New skill '{sk}' not present in the canonical resume")
    if check_proper_nouns:
        problems += proper_noun_violations(out_text, source_words(src))
    return problems


DEFAULT_ORDER = ["summary", "skills", "experience", "projects", "education", "achievements"]


def render_text(s: dict) -> str:
    """Plain-text rendering (used for diffs, the fact guard and DOCX/PDF export). Honours `section_order`."""
    h = dict(ATS_HEADINGS)
    h.update(s.get("headings") or {})
    out = [s.get("name") or ""]
    if s.get("headline") and s.get("show_headline", True):
        out.append(s["headline"])
    c = s.get("contact", {})
    out.append(" | ".join(x for x in [c.get("location"), c.get("email"), c.get("phone"), c.get("linkedin"),
                                      c.get("github")] if x))
    if s.get("tagline"):
        out.append(s["tagline"])
    for key in s.get("section_order") or DEFAULT_ORDER:
        if key == "summary" and s.get("summary"):
            out += ["", h["summary"], s["summary"]]
        elif key == "skills" and s.get("skills"):
            out += ["", h["skills"]]
            out += [f"{cat}: {', '.join(items)}" for cat, items in s["skills"].items()]
        elif key == "experience" and s.get("experience"):
            out += ["", h["experience"]]
            for r in s["experience"]:
                out.append(f"{r['title']} | {r['company']}{' | ' + r['location'] if r.get('location') else ''} | "
                           f"{r['start']} – {r['end']}")
                out += [f"• {b}" for b in r.get("bullets", []) if not re.search(r"\|", b)]
                for p in r.get("projects", []):
                    out.append(f"{p['name']} ({', '.join(p['tech'])})")
                    out += [f"• {b}" for b in p["bullets"]]
        elif key == "projects" and s.get("projects"):
            out += ["", h["projects"]]
            for p in s["projects"]:
                out.append(f"{p['name']}{' | ' + p['date'] if p.get('date') else ''}")
                if p.get("tech"):
                    out.append(", ".join(p["tech"]))
                out += [f"• {b}" for b in p["bullets"]]
        elif key == "education" and s.get("education"):
            out += ["", h["education"]]
            for e in s["education"]:
                out.append(" | ".join(x for x in [e.get("degree"), e.get("institution"),
                                                  f"{e.get('start_year')} – {e.get('end_year')}"
                                                  if e.get("start_year") else None,
                                                  f"CGPA: {e['gpa']}" if e.get("gpa") else None] if x))
        elif key == "achievements" and s.get("achievements"):
            out += ["", h["achievements"]]
            out += [f"• {a}" for a in s["achievements"]]
    return "\n".join(out)
