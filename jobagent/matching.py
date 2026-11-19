"""Transparent candidate-job matching. Pure functions; no AI. Every component explains itself."""
from __future__ import annotations

import re

from . import taxonomy
from .normalize import normalize_title, seniority
from .settings import COUNTRY_NAMES, country_tier
from .skills import GENERIC, IMPLIES, RELATED

NON_ENGINEERING = re.compile(
    r"\b(sales|account (executive|manager)|marketing|recruit(er|ing)|talent|designer|ux|ui designer|copywriter|"
    r"customer success|support specialist|legal|counsel|finance|accountant|hr\b|people partner|office manager|"
    r"product manager|program manager|project manager|scrum master|analyst(?! engineer)|data scientist|"
    r"researcher|nurse|driver|warehouse|teacher|consultant(?! engineer))\b", re.I)
ENGINEERING = re.compile(
    r"(software|developer|programmer|\bsde\b|\bswe\b|back[- ]?end|full[- ]?stack|front[- ]?end|\bweb\b|\.net\b|c#|"
    r"python|java\b|golang|\bgo\b|node|typescript|\bapi\b|application engineer|applications engineer|"
    r"integration engineer|platform engineer|ai engineer|ml engineer|machine learning engineer|llm|cloud engineer|"
    r"devops|site reliability|\bsre\b|data engineer|forward deployed|product engineer|solutions engineer|"
    r"test automation|automation engineer|qa engineer|mobile engineer|\bios\b|android|entwickler)", re.I)
# Engineering disciplines that are not software, even though the title says "Engineer".
NON_SOFTWARE_ENGINEERING = re.compile(
    r"\b(mechanical|electrical|electronics?|civil|chemical|patent|microwave|rf|radio frequency|hardware|"
    r"process engineer|field (service )?engineer|structural|manufacturing|mechatronics?|hvac|biomedical|aerospace|"
    r"optical|photonics|plasma|fusion|nuclear|sales engineer|pre-?sales|it support|it solutions|helpdesk|help desk|"
    r"technician|service engineer|maintenance|production engineer|quality engineer|validation engineer|"
    r"facilities|construction|piping|geotechnical|petroleum|mining|asic|fpga|analog|pcb|firmware|embedded)\b", re.I)


_STOP = set("""a about above after again all also an and any are as at be been being both but by can could did do does
doing during each few for from further had has have having he her here hers him his how i if in into is it its just
like may me might more most must my no nor not now of off on once only or other our ours out over own per please
same shall she should so some such than that the their them then there these they this those through to too under
until up upon us very via was we were what when where which while who whom why will with within would you your yours
role job team work working company candidate candidates experience years year looking join including ability strong
excellent good great new opportunity apply application position skills skill required requirements responsibilities
preferred plus etc well help make ensure provide support using use based day days week""".split())


def text_terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z+#&.-]{2,}", (text or "").lower()) if w not in _STOP}


def _strings(obj, out: list, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out, depth + 1)


def keyword_overlap(job_text: str, resume_terms: set, top: int = 30) -> tuple:
    """Share of the job description's most frequent meaningful words that also appear on the resume.
    Works for every industry (the skills catalogue only knows technical skills)."""
    words = [w for w in re.findall(r"[a-z][a-z+#&.-]{2,}", (job_text or "").lower()) if w not in _STOP]
    if not words:
        return 0.0, []
    freq: dict = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    key = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]
    hit = [w for w in key if w in resume_terms]
    return len(hit) / len(key), hit


def _resume_terms(structured) -> set:
    texts: list = []
    _strings(structured or {}, texts)
    return text_terms(" ".join(texts)[:200000])


def candidate_profile(resume_structured: dict, resume_analysis: dict, candidate: dict) -> dict:
    evidence = dict(resume_analysis.get("skill_evidence", {}))
    for sk, level in list(evidence.items()):
        for implied in IMPLIES.get(sk, ()):
            evidence.setdefault(implied, "implied")
    return {
        "skills": evidence,  # skill -> demonstrated | listed | implied
        "experience_years": round(resume_analysis.get("experience_months", 0) / 12, 1),
        "languages": {l["language"].lower(): l["level"] for l in candidate.get("languages", [])},
        "work_authorization": candidate.get("work_authorization", {}),
        "resume_terms": _resume_terms(resume_structured),
    }


def _skill_credit(skill: str, profile: dict) -> tuple[float, str]:
    level = profile["skills"].get(skill)
    if level == "demonstrated":
        return 1.0, "demonstrated in your experience"
    if level == "listed":
        return 0.6, "listed on your resume but not shown in a bullet"
    if level == "implied":
        return 0.5, "implied by related experience"
    related = [r for r in RELATED.get(skill, ()) if profile["skills"].get(r) in ("demonstrated", "listed")]
    if related:
        return 0.35, f"not on your resume; related experience: {', '.join(related)}"
    return 0.0, "not established by your resume"


def _converted_max(job: dict, settings: dict):
    """The job's top salary in the user's currency (None if it cannot be converted)."""
    from . import fx
    if job.get("salary_currency") == settings.get("currency"):
        return job.get("salary_max")
    return fx.convert(job["salary_max"], (job.get("salary_currency") or "").upper(), settings.get("currency") or "",
                      settings.get("_fx"))


LEVEL_YEARS = {"student": 0, "entry": 1, "mid": 3, "senior": 7, "manager": 8, "executive": 12}


def score_job(job: dict, analysis: dict, sponsorship: dict, profile: dict, settings: dict) -> dict:
    weights = settings["weights"]
    reasons, gaps, disq, notes = [], [], [], []
    career = settings.get("career") or {}
    career_on = taxonomy.has_career(career)       # False = the original software-engineering profile

    # ---------------- technical
    must, nice = analysis.get("must_have", []), analysis.get("nice_to_have", [])
    must_detail, nice_detail = [], []
    if must or nice:
        def avg(items, detail):
            if not items:
                return None
            tot, wsum = 0.0, 0.0
            for sk in items:
                c, why = _skill_credit(sk, profile)
                w = 0.5 if sk in GENERIC else 1.0
                tot += c * w
                wsum += w
                detail.append({"skill": sk, "credit": c, "why": why})
            return tot / wsum
        m, n = avg(must, must_detail), avg(nice, nice_detail)
        technical = (m if m is not None else 0.6) * 0.75 + (n if n is not None else (m or 0.6)) * 0.25
        strong = [d["skill"] for d in must_detail if d["credit"] >= 1.0 and d["skill"] not in GENERIC]
        if strong:
            reasons.append(f"You demonstrate required skills: {', '.join(strong[:6])}.")
        missing_must = [d["skill"] for d in must_detail if d["credit"] < 0.5]
        if missing_must:
            gaps.append(f"Required but not established: {', '.join(missing_must)}.")
        weak = [d["skill"] for d in must_detail if 0.5 <= d["credit"] < 1.0]
        if weak:
            gaps.append(f"Only partially shown: {', '.join(weak)}.")
    elif career_on and (job.get("industry") or "technology") != "technology":
        terms = profile.get("resume_terms") or set()
        if terms:
            frac, hit = keyword_overlap(job.get("description_text") or "", terms)
            technical = round(0.3 + 0.7 * min(1.0, frac * 1.6), 3)
            if hit:
                reasons.append(f"Your resume covers key words from this job: {', '.join(hit[:8])}.")
            if frac < 0.2:
                gaps.append("Few of this job's key words appear on your resume.")
        else:
            technical = 0.5
            notes.append("Upload your resume to compare your skills with this job (50% assumed).")
    else:
        technical = 0.3
        notes.append("No recognisable technical requirements were extracted; technical fit cannot be confirmed (30%).")

    # ---------------- experience & seniority
    cand_years = profile["experience_years"]
    my_level = career.get("experience") if career_on else None
    if my_level and not cand_years:
        cand_years = LEVEL_YEARS.get(my_level, 0)
    req = analysis.get("experience_years")
    level = seniority(job.get("title"))
    excluded_terms = [t for t in settings.get("excluded_title_terms", [])
                      if re.search(r"(?<![a-z])" + re.escape(t.strip()) + r"(?![a-z])", (job.get("title") or "").lower())]
    if my_level:
        fit = taxonomy.LEVEL_FIT[my_level].get(level, 0.3)
        if my_level in ("manager", "executive") and taxonomy.MANAGER_TITLE.search(normalize_title(job.get("title"))):
            fit = max(fit, 0.95)
        if excluded_terms:
            experience = 0.2
            disq.append(f"Title contains excluded term(s): {', '.join(excluded_terms)}.")
        elif fit <= 0.25:
            experience = fit
            disq.append(f"A {level}-level title does not suit your level ({taxonomy.EXPERIENCE[my_level][0]}).")
        else:
            experience = fit
            if req is not None and req - cand_years > 2:
                experience = min(experience, 0.3)
                gaps.append(f"Asks for {req:g}+ years; your level suggests ~{cand_years:g}.")
            elif req is not None and req <= cand_years:
                reasons.append(f"Requires {req:g}+ years; fits your experience.")
            if fit >= 0.9:
                reasons.append(f"Seniority fits your level ({taxonomy.EXPERIENCE[my_level][0]}).")
    elif level in ("senior", "staff", "principal", "lead"):
        experience = 0.1
        disq.append(f"Title indicates {level}-level role; you have ~{cand_years} years of experience.")
    elif excluded_terms:
        experience = 0.2
        disq.append(f"Title contains excluded term(s): {', '.join(excluded_terms)}.")
    elif req is None:
        experience = {"junior": 1.0, "unspecified": 0.8, "mid": 0.6, "intern": 0.3}.get(level, 0.7)
        if level == "mid":
            notes.append("Mid-level title without explicit years; review whether ~2 years is enough.")
    else:
        gap = req - cand_years
        if gap <= 0:
            experience = 1.0
            reasons.append(f"Requires {req:g}+ years; you have ~{cand_years}.")
        elif gap <= 1:
            experience = 0.6
            gaps.append(f"Asks for {req:g}+ years; you have ~{cand_years} (close).")
        elif gap <= 2:
            experience = 0.3
            gaps.append(f"Asks for {req:g}+ years; you have ~{cand_years}.")
        else:
            experience = 0.0
            disq.append(f"Requires {req:g}+ years of experience; you have ~{cand_years}.")
    wanted_types = (career.get("employment_types") or ["full_time"]) if career_on else ["full_time"]
    if level == "intern" or job.get("employment_type") == "internship":
        if "internship" not in wanted_types:
            disq.append("Internship / working-student position.")
    elif (career_on and job.get("employment_type") in taxonomy.EMPLOYMENT_TYPES
          and job.get("employment_type") not in wanted_types):
        gaps.append(f"{taxonomy.EMPLOYMENT_TYPES[job['employment_type']]} role; you asked for "
                    + ", ".join(taxonomy.EMPLOYMENT_TYPES[t] for t in wanted_types) + ".")

    # ---------------- sponsorship & work authorisation
    cls = sponsorship["classification"]
    if not sponsorship.get("needs_sponsorship", True):
        spons = 1.0
        reasons.append("No sponsorship needed (you can already work here or role is open to India).")
    else:
        spons = {"confirmed": 1.0, "likely": 0.65, "unknown": 0.3, "not_supported": 0.0}[cls]
        if cls == "confirmed":
            reasons.append("Posting explicitly offers visa sponsorship / work-permit support.")
        elif cls == "likely":
            reasons.append("Credible sponsorship pathway (see evidence), not confirmed for this role.")
        elif cls == "not_supported":
            disq.append("Posting says sponsorship is unavailable or existing right to work is required.")
        policy = settings.get("sponsorship_policy", "exclude_not_supported")
        if policy == "require_confirmed_or_likely" and cls == "unknown":
            disq.append("Sponsorship evidence unknown and your policy requires confirmed/likely.")
        if cls == "unknown":
            gaps.append("Sponsorship not mentioned; ask the recruiter before investing time.")

    # ---------------- role alignment
    title_n = normalize_title(job.get("title"))
    preferred = settings.get("preferred_roles", [])
    job_ind, job_role = job.get("industry"), job.get("role_key")
    if career_on and not job_ind:
        job_ind, job_role = taxonomy.classify(job.get("title"))
    if career_on:
        role, why = taxonomy.job_fit(career, job_ind, job_role, job.get("title"))
        (reasons if role >= 0.9 else gaps).append(why)
    elif NON_SOFTWARE_ENGINEERING.search(job.get("title") or ""):
        role = 0.0
        disq.append("Engineering discipline outside software (hardware / non-software engineering).")
    elif NON_ENGINEERING.search(job.get("title") or "") and not ENGINEERING.search(job.get("title") or ""):
        role = 0.0
        disq.append("Not a software engineering role.")
    elif any(p in title_n for p in preferred):
        role = 1.0
        reasons.append("Title matches your preferred roles.")
    elif ENGINEERING.search(job.get("title") or ""):
        role = 0.6
    else:
        role = 0.2
        gaps.append("Title does not clearly match your target roles.")
    if not career_on and re.search(r"\b(frontend|front end|ios|android|mobile|embedded|firmware|hardware|sre|devops|data engineer|"
                 r"machine learning engineer|security engineer|qa|test automation)\b", title_n) and role < 1.0:
        role = min(role, 0.4)
        notes.append("Specialisation differs from your backend/full-stack profile.")

    # ---------------- location & language
    country = job.get("country")
    tier = country_tier(settings, country)
    remote = job.get("remote_type")
    scope = job.get("remote_scope")
    if country == "IN" or (remote == "remote" and scope in ("india_allowed", "worldwide")):
        if settings.get("include_india_remote", True):
            loc = 0.7
            notes.append("India-based or India-eligible remote role (not an overseas relocation).")
        else:
            loc = 0.0
            disq.append("India / remote-from-India roles are excluded in Settings.")
    elif tier:
        loc = {1: 1.0, 2: 0.85, 3: 0.65}[tier]
        reasons.append(f"Located in a tier-{tier} target country ({COUNTRY_NAMES.get(country, country)}).")
        if remote == "remote" and scope == "europe_only":
            notes.append("Remote role limited to EU/Europe residents; usually requires an existing right to work.")
            loc = min(loc, 0.4)
    elif country is None:
        loc = 0.4
        notes.append("Country could not be determined from the listing.")
    else:
        loc = 0.0
        disq.append(f"Outside your target countries ({COUNTRY_NAMES.get(country, country)}).")
    allowed = settings.get("work_arrangements", ["onsite", "hybrid", "remote"])
    if remote in ("onsite", "hybrid", "remote") and remote not in allowed:
        disq.append(f"Work arrangement '{remote}' is not in your preferences.")
    lang_issues = []
    for lr in analysis.get("languages", []):
        lang = lr["language"].lower()
        if lang == "english":
            continue
        have = profile["languages"].get(lang)
        if lr["required"] and not have:
            lang_issues.append(lr["language"])
            disq.append(f"Requires {lr['language']}" + (f" ({lr['level']})" if lr.get("level") else "")
                        + " which is not on your profile.")
        elif not lr["required"] and not have:
            loc = max(0.0, loc - 0.15)
            gaps.append(f"{lr['language']} is a plus.")
    location_language = loc if not lang_issues else 0.0

    # ---------------- compensation
    min_salary = settings.get("min_salary", 0)
    if job.get("salary_max") and job.get("salary_currency"):
        if not min_salary:
            comp = 0.7
        elif job.get("salary_currency") != settings.get("currency") and _converted_max(job, settings) is None:
            comp = 0.5
            notes.append(f"Salary is in {job['salary_currency']}; not compared with your {settings.get('currency')} minimum.")
        elif (job.get("salary_period") or "year") == "year" and (_converted_max(job, settings) or 0) < min_salary:
            comp = 0.1
            gaps.append(f"Salary range tops out below your minimum ({min_salary:,} {settings.get('currency')})"
                        + (f"; about {int(_converted_max(job, settings) or 0):,} {settings.get('currency')} at today's"
                           f" exchange rate." if job.get("salary_currency") != settings.get("currency") else "."))
        else:
            comp = 1.0
    else:
        comp = 0.5

    components = {"technical": technical, "experience": experience, "sponsorship": spons, "role": role,
                  "location_language": location_language, "compensation": comp}
    wsum = sum(weights.values()) or 1
    score = round(100 * sum(weights[k] * components[k] for k in components) / wsum, 1)

    min_score = settings.get("min_match_score", 55)
    if disq:
        rec = "skip"
    elif score >= max(min_score, 68) and technical >= 0.5 and role >= 0.5:
        rec = "apply"
    elif technical < 0.45 and role >= 0.6 and score >= min_score - 15:
        rec = "improve_skills"
    elif score >= min_score - 10:
        rec = "review"
    else:
        rec = "skip"
        notes.append(f"Score {score} is below your review threshold ({min_score - 10}).")

    text = (job.get("description_text") or "").lower()
    effort = 20
    effort_notes = []
    if "cover letter" in text or "motivation letter" in text or "anschreiben" in text:
        effort += 15
        effort_notes.append("cover letter requested")
    if re.search(r"take[- ]home|coding (challenge|assignment|test)|technical assessment|hackerrank|codility", text):
        effort += 45
        effort_notes.append("technical assessment expected")
    if sponsorship.get("needs_sponsorship") and cls == "unknown":
        effort += 5
        effort_notes.append("confirm sponsorship with recruiter")

    return {
        "score": score,
        "recommendation": rec,
        "components": {k: round(v, 3) for k, v in components.items()},
        "weights": weights,
        "reasons": reasons,
        "gaps": gaps,
        "disqualifiers": disq,
        "notes": notes,
        "must_have": must_detail,
        "nice_to_have": nice_detail,
        "effort_minutes": effort,
        "effort_notes": effort_notes,
        "seniority": level,
    }
