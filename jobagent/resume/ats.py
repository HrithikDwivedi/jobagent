"""Transparent, deterministic ATS score (0-100) for a tailored resume, modelled on how ATS parsers and
Jobscan-style keyword checkers grade. Weights and thresholds live in styles.json ("ats").

Components: parseability (PDF round-trip through our own parser), hard-skill keyword match (JD surface forms get
full credit, synonyms partial), job-title match, measurable results, sections/headings, length & formatting.
The score never rewards untrue content: keywords the candidate lacks are reported as missing/blockers."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from ..skills import SKILLS, find_skills
from .styles import styles
from .variants import render_text

_NUM = re.compile(r"\d")
_DATE_FORMS = {"mon_yyyy": r"^[A-Z][a-z]{2} \d{4}$", "month_yyyy": r"^[A-Z][a-z]+ \d{4}$", "mm_yyyy": r"^\d{2}/\d{4}$",
               "yyyy": r"^\d{4}$"}


def conf() -> dict:
    return styles()["ats"]


# ------------------------------------------------------------------ JD surface forms & aliases

def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip(" ,;:()").rstrip(".")


def surface_forms(job_text: str | None) -> dict[str, list[str]]:
    """{canonical skill: [exact wordings used in the JD]} (e.g. '.NET' -> ['.NET Core'])."""
    out: dict[str, list[str]] = {}
    for sk, snippets in find_skills(job_text or "").items():
        forms = []
        for sn in snippets:
            c = _clean(sn)
            if c and c.lower() not in {f.lower() for f in forms}:
                forms.append(c)
        out[sk] = forms
    return out


def phrase_in(phrase: str, text: str) -> bool:
    """Case-insensitive whole-phrase match, singular/plural tolerant ('LLM' ~ 'LLMs', 'API' ~ 'APIs')."""
    p = phrase.strip()
    stem = p[:-1] if len(p) > 3 and p.lower().endswith("s") else p
    rx = r"(?<![A-Za-z0-9])" + re.escape(stem) + r"s?(?![A-Za-z0-9])"
    return bool(re.search(rx, text or "", re.I))


def alias_ok(alias: str, skill: str, held: set[str]) -> bool:
    """An alias may be inserted only if it denotes `skill` (via the skills taxonomy patterns), every skill it
    mentions is held by the candidate, and it adds no digits."""
    found = set(find_skills(alias))
    return bool(found) and skill in found and found <= held and not _NUM.search(alias) and len(alias) <= 40


# ------------------------------------------------------------------ title helpers

def clean_title(title: str | None) -> str:
    """Posting title minus seniority/level words, team/location suffixes and gender markers."""
    t = title or ""
    t = re.sub(r"\((?:[^()]*\b(?:m/f|f/m|m/w|w/m|d|x|team|remote|hybrid|onsite|contract)\b[^()]*)\)", "", t, flags=re.I)
    t = re.split(r"\s+[-–—|,:]\s+|\s*\|\s*", t)[0]
    # keep parentheses that name a skill, e.g. '(.NET)'; drop others, e.g. '(Consulting)'
    t = re.sub(r"\s*\(([^()]*)\)", lambda m: m.group(0) if find_skills(m.group(1)) else "", t)
    strip = set(conf()["title_strip_words"])
    words = [w for w in t.split() if w.lower().strip(".,") not in strip]
    return " ".join(words).strip()


def title_is_truthful(title: str, canonical_skills: set[str]) -> bool:
    """True when every word is a generic role word or a skill the candidate holds, with no seniority claim."""
    c = conf()
    low = title.lower()
    if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) for w in c["title_forbidden_words"]):
        return False
    rest = low
    for sk in find_skills(title):
        if sk not in canonical_skills:
            return False
        rest = re.sub(SKILLS_PATTERN(sk), " ", rest, flags=re.I)
    toks = [t for t in re.findall(r"[a-z.#&/+-]+", rest) if t.strip(".-")]
    role = set(c["title_role_words"])
    return bool(toks) and all(t.strip("().") in role for t in toks) and any(
        w in low for w in c["title_core_words"])


def SKILLS_PATTERN(skill: str) -> str:  # noqa: N802 - small helper
    pats = SKILLS.get(skill, ("", []))[1]
    return r"(?<![A-Za-z0-9])(?:" + "|".join(pats) + r")(?![A-Za-z0-9])" if pats else r"(?!x)x"


def headline_for(title: str | None, family_headline: str, canonical_skills: set[str]) -> tuple[str, str]:
    """(headline, kind): the posting's own (cleaned) title when truthful, else the family headline with
    'targeting <title>'. kind in {'posting', 'targeting', 'family'}."""
    ct = clean_title(title)
    if not ct:
        return family_headline, "family"
    if title_is_truthful(ct, canonical_skills):
        return ct, "posting"
    return conf()["headline_targeting"].format(headline=family_headline, title=ct), "targeting"


# ------------------------------------------------------------------ scoring

def _bullets(s: dict) -> list[str]:
    out = []
    for r in s.get("experience", []):
        out += r.get("bullets", [])
        for p in r.get("projects", []):
            out += p.get("bullets", [])
    for p in s.get("projects", []):
        out += p.get("bullets", [])
    return out


def _component(score: float, mx: float, details: list[str]) -> dict:
    return {"score": round(max(0.0, min(mx, score)), 2), "max": mx, "details": details}


def _parseability(s: dict, pdf: bytes, mx: float) -> dict:
    import pymupdf

    from .parser import ResumeError, parse
    c = conf()
    checks: list[tuple[str, bool]] = []
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    fonts = {f[3].split("+")[-1] for p in doc for f in p.get_fonts()}
    doc.close()
    checks.append(("text layer present", len(text.strip()) > 200))
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "resume.pdf"
            p.write_bytes(pdf)
            parsed = parse(p)
    except ResumeError as e:
        return _component(0, mx, [f"FAIL round-trip parse: {e.message}"])
    pc = parsed.get("contact", {})
    name = (s.get("name") or "").lower()
    checks.append(("name parsed", (parsed.get("name") or "").lower() == name))
    sc = s.get("contact", {})
    checks.append(("email parsed", bool(pc.get("email")) and pc.get("email") == sc.get("email")))
    checks.append(("phone parsed", bool(pc.get("phone"))))
    checks.append(("LinkedIn parsed", not sc.get("linkedin") or bool(pc.get("linkedin"))))
    order = [k for k in (s.get("section_order") or []) if s.get(k)]
    found = parsed.get("raw_headings", {})
    missing = [k for k in order if k not in found]
    checks.append(("all sections detected" + (f" (missing {', '.join(missing)})" if missing else ""), not missing))
    roles = parsed.get("experience", [])
    checks.append(("every role has parseable dates",
                   len(roles) >= len(s.get("experience", [])) and all(r.get("start") for r in roles)))
    n_parsed = sum(len(r.get("bullets", [])) + sum(len(p["bullets"]) for p in r.get("projects", []))
                   for r in roles) + sum(len(p.get("bullets", [])) for p in parsed.get("projects", []))
    n_bul = sum(len(r.get("bullets", [])) + sum(len(p["bullets"]) for p in r.get("projects", []))
                for r in s.get("experience", [])) + sum(len(p.get("bullets", [])) for p in s.get("projects", []))
    checks.append(("bullets preserved", n_parsed >= n_bul))
    checks.append(("no stray glyphs", not any(g in text for g in c["stray_glyphs"])))
    checks.append(("single column", not parsed.get("layout", {}).get("two_column")))
    lines = [ln.strip().lower() for ln in text.splitlines()]
    h = s.get("headings") or {}

    def line_of(head: str) -> int:
        return next((i for i, ln in enumerate(lines) if ln == head.lower()), -1)
    pos = [line_of(h.get(k) or k) for k in order]
    checks.append(("reading order intact", all(x >= 0 for x in pos) and pos == sorted(pos)))
    std = set(c["standard_fonts"])
    checks.append(("standard fonts" + ("" if fonts <= std else f" (found {', '.join(sorted(fonts - std))})"),
                   fonts <= std))
    ok = sum(1 for _, v in checks if v)
    return _component(mx * ok / len(checks), mx, [("OK " if v else "FAIL ") + n for n, v in checks])


def _keywords(text: str, jd: dict | None, job_text: str | None, canonical_skills: set[str], mx: float
              ) -> tuple[dict, list[str], list[dict], list[str]]:
    c = conf()
    jd = jd or {}
    forms = surface_forms(job_text)
    present = set(find_skills(text))
    items = [(s, c["must_have_weight"], True) for s in jd.get("must_have", [])] + \
            [(s, c["nice_to_have_weight"], False) for s in jd.get("nice_to_have", []) if s not in jd.get("must_have", [])]
    if not items:
        return _component(mx, mx, ["no JD keywords to match"]), [], [], []
    total = earned = 0.0
    details, matched, missing, blockers = [], [], [], []
    for sk, w, must in items:
        total += w
        wording = [f for f in forms.get(sk, []) if f]
        exact = [f for f in wording if phrase_in(f, text)]
        if sk in present and (exact or not wording):
            earned += w
            matched.append(sk)
            details.append(f"OK {sk}" + (f" (JD wording '{exact[0]}')" if exact else ""))
        elif sk in present:
            earned += w * c["synonym_credit"]
            matched.append(sk)
            details.append(f"PARTIAL {sk}: synonym only; JD says '{wording[0]}'")
        else:
            held = sk in canonical_skills
            reason = "in your master resume but not on this page" if held else "not on your resume"
            missing.append({"keyword": sk, "reason": reason, "required": must})
            details.append(f"MISSING {sk} ({reason})")
            if must and not held:
                blockers.append(f"Required skill '{sk}' is not on your resume and cannot be added truthfully.")
    return _component(mx * earned / total, mx, details), matched, missing, blockers


def ats_score(structured: dict, layout: dict | None, jd: dict | None, job_title: str | None, canonical: dict,
              job_text: str | None = None, target: int | None = None, pdf: bytes | None = None) -> dict:
    """Score a (tailored) resume. Returns {"score", "components", "matched_keywords", "missing_keywords",
    "blockers", "target"}."""
    from .export import export_pdf, pdf_metrics
    c = conf()
    wts = c["weights"]
    s = structured
    pdf = pdf or export_pdf(s, layout)
    text = render_text(s)
    canon_text = canonical.get("text") or render_text(canonical)
    canon_skills = set(find_skills(canon_text))
    comps: dict[str, dict] = {}
    blockers: list[str] = []

    comps["parseability"] = _parseability(s, pdf, wts["parseability"])

    comps["keywords"], matched, missing, kb = _keywords(text, jd, job_text, canon_skills, wts["keywords"])
    blockers += kb

    ct = clean_title(job_title)
    head_text = ((s.get("headline") or "") + " " + (s.get("summary") or "")[:300]).lower()
    if not ct:
        comps["title"] = _component(wts["title"], wts["title"], ["no target title"])
    elif ct.lower() in head_text:
        comps["title"] = _component(wts["title"], wts["title"], [f"OK title '{ct}' in headline/summary"])
    else:
        toks = [t for t in re.findall(r"[a-z.#+]+", ct.lower()) if len(t) > 1]
        core = any(w in head_text for w in c["title_core_words"] if w in ct.lower())
        hit = sum(1 for t in toks if t in head_text)
        frac = 0.5 if core and hit >= max(1, len(toks) - 1) else (0.25 if core else 0.0)
        comps["title"] = _component(wts["title"] * frac, wts["title"], [f"PARTIAL title '{ct}' not verbatim"])
        if not title_is_truthful(ct, canon_skills):
            blockers.append(f"The posting title '{ct}' is not a title you have held; it is shown only as a target.")

    bullets = _bullets(s)
    ratio = (sum(1 for b in bullets if _NUM.search(b)) / len(bullets)) if bullets else 0.0
    comps["metrics"] = _component(wts["metrics"] * min(1.0, ratio / c["metric_full_ratio"]), wts["metrics"],
                                  [f"{ratio:.0%} of bullets contain a measurable result (full credit at "
                                   f"{c['metric_full_ratio']:.0%})"])
    if ratio < c["metric_full_ratio"]:
        blockers.append(f"Only {ratio:.0%} of bullets have a metric; add real numbers to the master resume "
                        "(metrics are never invented).")

    std = set()
    for v in styles()["headings"].values():
        std.add(v.lower())
    for prof in styles()["countries"].values():
        std |= {v.lower() for v in prof.get("headings", {}).values()}
    for ct_ in styles()["company_types"].values():
        std |= {v.lower() for v in ct_.get("headings", {}).values()}
    h = s.get("headings") or {}
    need = ["summary", "skills", "experience", "education"] + (["projects"] if canonical.get("projects") else [])
    checks = [(f"{k} section present", bool(s.get(k))) for k in need]
    checks += [(f"standard heading for {k}", (h.get(k) or "").lower() in std) for k in need if s.get(k)]
    con = s.get("contact", {})
    checks += [(f"contact {k}", bool(con.get(k))) for k in ("email", "phone", "linkedin")]
    checks.append(("location in header", bool(con.get("location") or s.get("tagline"))))
    ok = sum(1 for _, v in checks if v)
    comps["sections"] = _component(wts["sections"] * ok / len(checks), wts["sections"],
                                   [("OK " if v else "FAIL ") + n for n, v in checks])

    m = pdf_metrics(pdf)
    import pymupdf
    d = pymupdf.open(stream=pdf, filetype="pdf")
    images = sum(len(p.get_images()) for p in d)
    d.close()
    maxc = int((layout or {}).get("bullet_max_chars") or s.get("bullet_max_chars") or 260)
    roles_ok = True
    for r in s.get("experience", []):
        groups = ([r["bullets"]] if r.get("bullets") else []) + [p["bullets"] for p in r.get("projects", [])]
        total = sum(len(g) for g in groups)
        if total < min(c["role_bullets_min"], total or 1) or any(len(g) > c["group_bullets_max"] for g in groups):
            roles_ok = False
    if s.get("experience") and sum(len(g) for r in s["experience"][:1]
                                   for g in [r.get("bullets", [])] + [p["bullets"] for p in r.get("projects", [])]) \
            < c["role_bullets_min"]:
        roles_ok = False
    dates = [x for r in s.get("experience", []) for x in (r.get("start"), r.get("end"))
             if x and x.lower() not in ("present", "current", "now")]
    fmts = {next((k for k, rx in _DATE_FORMS.items() if re.match(rx, x)), "other") for x in dates}
    fmts.discard("yyyy")
    checks = [("exactly one page", m["pages"] == 1, 4),
              (f"bullets <= {maxc} characters", all(len(b) <= maxc for b in bullets), 2),
              (f"{c['role_bullets_min']}-{c['group_bullets_max']} bullets per role", roles_ok, 2),
              ("no tables/images", images == 0, 1),
              (f"file size < {c['max_file_kb']} KB", len(pdf) < c["max_file_kb"] * 1024, 0.5),
              ("consistent date format", len(fmts) <= 1, 0.5)]
    tot = sum(w for _, _, w in checks)
    got = sum(w for _, v, w in checks if v)
    comps["formatting"] = _component(wts["formatting"] * got / tot, wts["formatting"],
                                     [("OK " if v else "FAIL ") + n for n, v, _ in checks])
    if m["pages"] != 1:
        blockers.append("Resume does not fit on one page.")

    score = sum(cp["score"] for cp in comps.values())
    return {"score": int(round(score)), "score_exact": round(score, 2), "components": comps,
            "matched_keywords": matched, "missing_keywords": missing, "blockers": list(dict.fromkeys(blockers)),
            "target": int(target if target is not None else c["target"])}
