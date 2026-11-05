"""H. Job/country/company-aware one-page resume tailoring (tailor.py, styles.json, export.py, store integration).

Uses the REAL resume read-only (skipped when absent) plus SYNTHETIC resumes. AI is a FAKE in-process provider
unless JOBAGENT_LIVE_AI=1 (opt-in, costs a few tenths of a cent)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

import pymupdf
import pytest

from factories import ADMIN, DOTNET_JD, REAL_RESUME, SYNTHETIC_RESUME_LINES, build_pdf
from jobagent import settings as settings_mod
from jobagent.ai import AIService, Completion, Provider
from jobagent.jd import analyze as analyze_jd
from jobagent.resume import store
from jobagent.resume.analysis import analyze
from jobagent.resume.export import export_docx, export_pdf, page_count
from jobagent.resume.parser import parse
from jobagent.resume.styles import country_profile, format_date, localise_spelling, styles
from jobagent.resume.tailor import resolve_style, shorten, tailor
from jobagent.resume.variants import render_text, verify_no_new_facts
from wf import add_job

# SYNTHETIC candidate facts (shape of the user's Settings; no real contact data). DEFAULT_SETTINGS ships blank.
CAND = dict(settings_mod.DEFAULT_SETTINGS["candidate"],
            full_name="Hrithik Rajendra Dwivedi", email="candidate.example@example.com", phone="+91 90000 00001",
            linkedin_url="https://www.linkedin.com/in/hrithik-dwivedi/", current_location="Mumbai, India",
            nationality="Indian", citizenship_countries=["IN"], work_authorization={"IN": "citizen"},
            requires_sponsorship_outside=True, willing_to_relocate=True)
NO_AI = dict(settings_mod.DEFAULT_SETTINGS["resume_style"], ai_rewrite=False)

AI_JD = ("We are hiring an AI Engineer to build LLM-powered products.\nRequirements\n"
         "• Python and FastAPI\n• Experience with LLMs, RAG and prompt engineering\n"
         "• REST APIs\nNice to have\n• Speech AI (Whisper)\n")


# ----------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def real():
    if not REAL_RESUME.is_file():
        pytest.skip(f"Real resume not present at {REAL_RESUME}")
    before = hashlib.sha256(REAL_RESUME.read_bytes()).hexdigest()
    s = parse(REAL_RESUME)
    yield s, analyze(s, CAND, ["IE", "GB", "CA", "AU", "AE"])
    assert hashlib.sha256(REAL_RESUME.read_bytes()).hexdigest() == before, "real resume was modified"


@pytest.fixture(scope="module")
def synthetic():
    with tempfile.TemporaryDirectory() as d:
        return parse(build_pdf(Path(d) / "a.pdf"))


def _long_lines():
    """SYNTHETIC long resume: 4 roles x 7 long bullets, way over one page."""
    lines = list(SYNTHETIC_RESUME_LINES[:10])
    for k in range(4):
        lines.append((f"Software Engineer {k}   Jan {2015 + 2 * k} - Dec {2016 + 2 * k}", "b"))
        lines.append((f"Synthetic Corp {k} Ltd | Pune, India", "n"))
        for j in range(7):
            lines.append((f"• Built ASP.NET Core and Python services batch {k}{j} handling {100 + j * 37} requests "
                          f"per second with SQL Server and Docker on AWS", "n"))
    return lines + SYNTHETIC_RESUME_LINES[18:]


@pytest.fixture(scope="module")
def long_synthetic():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "long.pdf"
        c = canvas.Canvas(str(p), pagesize=A4)
        y = 800
        for text, style in _long_lines():
            if y < 60:
                c.showPage()
                y = 800
            c.setFont("Helvetica-Bold" if style in ("h", "b") else "Helvetica", 9.5)
            c.drawString(40, y, text)
            y -= 14
        c.save()
        s = parse(p)
    assert sum(len(r["bullets"]) for r in s["experience"]) >= 28
    return s


def _pages(res) -> int:
    return page_count(export_pdf(res.structured, res.layout))


def _all_bullets(st: dict) -> list[str]:
    out = []
    for r in st.get("experience", []):
        out += r.get("bullets", [])
        for p in r.get("projects", []):
            out += p["bullets"]
    for p in st.get("projects", []):
        out += p["bullets"]
    return out


# ----------------------------------------------------------------------------- one page

COMBOS = [("dotnet_backend", "IE"), ("ai_application", "GB"), ("python_backend", "DE"), ("fullstack", "CA"),
          ("integration", "AU"), ("dotnet_backend", "AE"), ("ai_application", "JP"), ("python_backend", "US"),
          ("fullstack", "IN")]


@pytest.mark.parametrize("family,country", COMBOS)
def test_real_resume_one_page_and_no_new_facts(real, family, country):
    """Expected: every role family x country tailors the real resume to exactly ONE page, with no new
    numbers/skills/proper nouns, and the page size follows the country profile (Letter for US/CA)."""
    s, a = real
    res = tailor(s, a, CAND, NO_AI, role_family=family, country=country)
    assert res.layout["pages"] == 1 and _pages(res) == 1
    assert res.guard_problems == []
    assert verify_no_new_facts(s, res.structured, CAND, res.style["guard_allowed_text"],
                               check_proper_nouns=True) == []
    pdf = pymupdf.open(stream=export_pdf(res.structured, res.layout))
    width = pdf[0].rect.width
    assert (abs(width - 612) < 1) if country in ("US", "CA") else (abs(width - 595.3) < 1)
    # never drops the current role header, contact info or education
    st = res.structured
    assert st["experience"][0]["title"] == s["experience"][0]["title"]
    assert st["contact"]["email"] == s["contact"]["email"] and st["education"]


@pytest.mark.parametrize("ctype", ["big_tech", "enterprise", "startup"])
def test_long_synthetic_resume_fits_one_page(long_synthetic, ctype):
    """Expected: a 4-role, 28-bullet SYNTHETIC resume is compacted to one page for every company type,
    keeping every role header and the education section."""
    res = tailor(long_synthetic, None, CAND, dict(NO_AI, company_type=ctype), role_family="python_backend",
                 country="US")
    assert _pages(res) == 1 and res.layout["pages"] == 1
    assert len(res.structured["experience"]) == 4 and res.structured["education"]
    assert res.guard_problems == []
    assert res.layout["font_size"] >= 9.0 and res.layout["margin_x_cm"] >= 1.2


def test_short_synthetic_resume_keeps_roomy_layout(synthetic):
    """Expected: a short SYNTHETIC resume needs no compaction (largest preset) and no content is dropped."""
    res = tailor(synthetic, None, CAND, NO_AI, role_family="dotnet_backend", country="IE")
    assert res.layout["ladder_step"] == 0 and _pages(res) == 1
    assert len(_all_bullets(res.structured)) == len(_all_bullets(synthetic))
    assert res.guard_problems == []


# ----------------------------------------------------------------------------- analysis issues fixed

def test_all_analysis_issues_fixed_overseas(real):
    """Expected: Resume Studio issues are fixed for an overseas target: heading, LinkedIn, India-only targeting,
    sponsorship line, glyphs, 'vibe coding', bullet length, awards heading, single column."""
    s, a = real
    res = tailor(s, a, CAND, NO_AI, role_family="dotnet_backend", country="IE")
    st = res.structured
    text = render_text(st)
    pdf_text = pymupdf.open(stream=export_pdf(st, res.layout))[0].get_text()
    for t in (text, pdf_text):
        assert "E XPERIENCE" not in t
        assert "linkedin.com/in/hrithik-dwivedi" in t and "hrithikdwivedi" not in t
        assert not re.search(r"anywhere in india", t, re.I)
        assert "Requires visa sponsorship" in t and "Indian national" in t
        assert "▸" not in t and "★" not in t
        assert not re.search(r"vibe coding", t, re.I)
    assert st["headings"]["achievements"] == "Awards & Recognition"
    assert st["headings"]["experience"] == "Work Experience"
    maxc = styles()["company_types"][res.style["company_type"]]["bullet_max_chars"]
    assert all(len(b) <= maxc for b in _all_bullets(st))
    assert not any(re.search(r"\bseeking\b", x, re.I) for x in [st["summary"], st["tagline"]])
    reasons = " ".join(c["reason"] for c in res.changes)
    assert "Single column" in json.dumps(res.changes) and "letter-spaced" in reasons
    openers = [b.split()[0] for b in _all_bullets(st)]
    assert openers.count("Implemented") <= 1


def test_india_target_has_no_sponsorship_line(real):
    """Expected: for India (citizen) the header has no nationality/sponsorship statement."""
    s, a = real
    res = tailor(s, a, CAND, NO_AI, role_family="dotnet_backend", country="IN")
    assert "sponsorship" not in res.structured["tagline"].lower()
    assert "national" not in res.structured["tagline"].lower()
    assert res.structured["tagline"].startswith("Mumbai, India")


def test_spelling_localised_per_country(real):
    """Expected: UK spelling for GB ('optimised') and US spelling for US ('optimized')."""
    s, a = real
    gb = render_text(tailor(s, a, CAND, NO_AI, role_family="dotnet_backend", country="GB").structured)
    us = render_text(tailor(s, a, CAND, NO_AI, role_family="dotnet_backend", country="US").structured)
    assert re.search(r"optimised|optimisation", gb, re.I) and not re.search(r"optimiz", gb, re.I)
    assert re.search(r"optimized|optimization", us, re.I) and not re.search(r"optimis", us, re.I)
    # explicit setting overrides the country default
    forced = tailor(s, a, CAND, dict(NO_AI, spelling="us"), role_family="dotnet_backend", country="GB")
    assert "optimis" not in render_text(forced.structured).lower()
    assert localise_spelling("Optimised the behaviour", "us") == "Optimized the behavior"
    assert localise_spelling("Optimized enrollment", "uk") == "Optimised enrolment"


def test_country_profiles_are_data_driven():
    """Expected: every required country has a resolved profile; DE dates are MM/YYYY; US/CA use Letter."""
    for code in ["IE", "GB", "DE", "NL", "SE", "DK", "FI", "AT", "CH", "BE", "LU", "FR", "ES", "PT", "IT", "PL",
                 "CZ", "EE", "CA", "AU", "AE", "JP", "IN", "US"]:
        p = country_profile(code)
        assert p["code"] == code and p["spelling"] in ("uk", "us") and p["page_size"] in ("A4", "Letter")
        assert "photo" in p["never_include"] and p["headings"].get("experience")
    assert country_profile("US")["page_size"] == "Letter" and country_profile("CA")["page_size"] == "Letter"
    assert country_profile("GB")["header"]["nationality"] and not country_profile("US")["header"]["nationality"]
    assert format_date("July 2024", "mm_yyyy") == "07/2024" and format_date("July 2024", "mon_yyyy") == "Jul 2024"
    assert country_profile(None)["code"] is None


# ----------------------------------------------------------------------------- style resolution

def test_style_resolution_auto_and_override():
    """Expected: auto detects big_tech for Stripe and startup for 'Series A, seed-stage'; nothing detected falls
    back to big_tech (MNC); an explicit setting always wins over auto-detection."""
    st = resolve_style({"company_type": "auto"}, country="IE", employer="Stripe")
    assert st["company_type"] == "big_tech" and st["company_type_source"] == "detected"
    st = resolve_style({"company_type": "auto"}, job={"title": "Engineer", "employer": "Tiny Labs",
                                                     "description_text": "We are a Series A, seed-stage startup."})
    assert st["company_type"] == "startup"
    st = resolve_style({"company_type": "auto"}, job={"title": "Engineer", "description_text": "Build things."})
    assert st["company_type"] == "big_tech" and st["company_type_source"] == "default"
    st = resolve_style({"company_type": "auto"}, job={"title": "Engineer", "employer": "Some Bank Plc",
                                                     "description_text": "A regulated banking group, est. 1890."})
    assert st["company_type"] == "enterprise"
    st = resolve_style({"company_type": "enterprise"}, employer="Stripe")
    assert st["company_type"] == "enterprise" and st["company_type_source"] == "setting"
    assert resolve_style({}, country="GB")["spelling"] == "uk" and resolve_style({}, country="US")["spelling"] == "us"


# ----------------------------------------------------------------------------- relevance

def _mini():
    s = {"name": "Sam Synthetic", "contact": {"email": "sam@example.com"}, "summary": "",
         "skills": {"Languages": ["C#", "Python"]},
         "experience": [{"title": "Software Engineer", "company": "Synthetic Co", "location": None,
                         "start": "Jan 2023", "end": "Present", "projects": [], "bullets": [
                             "Organised weekly knowledge-sharing sessions for 12 engineers",
                             "Built a RAG chatbot with OpenAI GPT LLMs and Python answering 300 support queries daily",
                             "Built ASP.NET Core Web API services in C# with Entity Framework serving 2000 users"]}],
         "projects": [], "education": [], "achievements": []}
    s["text"] = render_text(s)
    return s


def test_relevance_orders_bullets_for_the_job():
    """Expected: a .NET job puts the .NET bullet first; an AI job puts the RAG/LLM bullet first."""
    s = _mini()
    dn = tailor(s, None, CAND, NO_AI, job={"title": ".NET Developer", "description_text": DOTNET_JD, "country": "IE"})
    assert "ASP.NET" in dn.structured["experience"][0]["bullets"][0]
    assert dn.structured["role_family"] == "dotnet_backend"
    ai = tailor(s, None, CAND, NO_AI, job={"title": "AI Engineer", "description_text": AI_JD, "country": "GB"})
    assert "RAG" in ai.structured["experience"][0]["bullets"][0]
    assert ai.structured["role_family"] == "ai_application"


def test_relevance_real_resume_ai_job(real):
    """Expected: for an AI job the RAG chatbot bullet leads the first project and QGen AI leads Projects."""
    s, a = real
    res = tailor(s, a, CAND, NO_AI, job={"title": "AI Engineer", "description_text": AI_JD, "country": "GB",
                                         "employer": "Monzo"})
    first = res.structured["experience"][0]["projects"][0]["bullets"][0]
    assert re.search(r"RAG|retrieval-augmented|GPT", first)
    assert res.structured["projects"][0]["name"].startswith("QGen AI")


def test_shorten_keeps_numbers():
    """Expected: shortening drops enumerations/trailing clauses but never a number."""
    b = ("Architected and deployed 4 master modules (Business Management Products, Sales Costs, Other Costs, Cost "
         "of Delivery), enabling structured data configuration and streamlined reporting for 500+ users")
    out, notes = shorten(b, 140)
    assert len(out) <= 140 and "4" in out and "500" in out and "parenthetical" in " ".join(notes)


# ----------------------------------------------------------------------------- export

def test_role_line_dates_extract_on_one_line(real):
    """Expected: the role title, company and right-aligned dates are extracted by pymupdf as ONE line."""
    s, a = real
    res = tailor(s, a, CAND, NO_AI, role_family="dotnet_backend", country="IE")
    lines = pymupdf.open(stream=export_pdf(res.structured, res.layout))[0].get_text().splitlines()
    hit = [ln for ln in lines if "Software Engineer" in ln and "TeamLease EdTech" in ln]
    assert hit and "Jul 2024" in hit[0] and "Present" in hit[0]
    words = pymupdf.open(stream=export_pdf(res.structured, res.layout))[0].get_text("words")
    date_w = [w for w in words if w[4] == "Present"][0]
    page_w = pymupdf.open(stream=export_pdf(res.structured, res.layout))[0].rect.width
    assert date_w[2] > page_w * 0.85, "dates must be right-aligned"


def test_docx_generated_and_opens(real):
    """Expected: DOCX export opens with python-docx, mirrors the selection, and uses a right tab for dates."""
    import docx
    s, a = real
    res = tailor(s, a, CAND, NO_AI, role_family="dotnet_backend", country="IE")
    d = docx.Document(io.BytesIO(export_docx(res.structured, res.layout)))
    texts = [p.text for p in d.paragraphs]
    assert texts[0] == "Hrithik Rajendra Dwivedi"
    role = [p for p in d.paragraphs if p.text.startswith("Software Engineer") and "TeamLease" in p.text][0]
    assert "\tJul 2024 – Present" in role.text
    assert role.paragraph_format.tab_stops[0].alignment == 2  # WD_TAB_ALIGNMENT.RIGHT
    n_bullets = sum(1 for p in d.paragraphs if p.style.name == "List Bullet")
    assert n_bullets == len(_all_bullets(res.structured)) + len(res.structured["achievements"])


def test_export_backwards_compatible(synthetic):
    """Expected: export_pdf/export_docx still accept a plain structured dict without a layout."""
    assert export_pdf(synthetic).startswith(b"%PDF")
    assert export_docx(synthetic).startswith(b"PK")


# ----------------------------------------------------------------------------- AI guard (FAKE provider)

class FakeTailorProvider(Provider):
    """SYNTHETIC model that tries to inject a fake metric, a fake skill and a fake employer."""
    name = "fake"

    def __init__(self):
        self.calls = 0

    def complete_json(self, model, system, user, max_tokens):
        self.calls += 1
        m = re.search(r"<resume_bullets>\n(.*)\n</resume_bullets>", user, re.S)
        items = json.loads(m.group(1))
        out = []
        for i, it in enumerate(items):
            words = it["text"].split()
            if i == 0:
                t = "Increased revenue by 300% by " + " ".join(words[1:8]).lower()
            elif i == 1:
                t = "Built Kubernetes clusters and " + " ".join(words[1:10]).lower()
            elif i == 2:
                t = " ".join(words[:9]) + " at Globex Corporation"
            else:
                t = "Shipped " + " ".join(words[1:])        # legitimate verb change
            out.append({"id": it["id"], "text": t})
        summary = "Software engineer with 2+ years who increased revenue by 300% using Kubernetes at Globex."
        return Completion(json.dumps({"bullets": out, "summary": summary}), 1000, 400, model)


def test_fake_ai_injection_rejected(db, real):
    """Expected: bullets with an invented metric, skill or employer are rejected (deterministic text kept),
    legitimate rewrites are accepted, and the final resume contains none of the injected facts."""
    s, a = real
    prov = FakeTailorProvider()
    ai = AIService(db, prov, settings_mod.DEFAULT_SETTINGS["ai"], user_id=ADMIN)
    res = tailor(s, a, CAND, dict(NO_AI, ai_rewrite=True), role_family="dotnet_backend", country="IE", ai=ai)
    text = render_text(res.structured)
    assert prov.calls == 1
    assert "300" not in text and "Kubernetes" not in text and "Globex" not in text
    w = " ".join(res.warnings)
    assert "new number(s) 300" in w and "new skill(s) Kubernetes" in w and "Globex" in w
    assert "AI summary rejected" in w
    assert res.method == "ai" and any(b.startswith("Shipped ") for b in _all_bullets(res.structured))
    assert res.guard_problems == [] and _pages(res) == 1


def test_ai_unavailable_falls_back_to_rules(db, real):
    """Expected: with no provider the pipeline runs rule-based only (no error, method='rules')."""
    s, a = real
    ai = AIService(db, Provider(), settings_mod.DEFAULT_SETTINGS["ai"], user_id=ADMIN)
    res = tailor(s, a, CAND, dict(NO_AI, ai_rewrite=True), role_family="dotnet_backend", country="IE", ai=ai)
    assert res.method == "rules" and _pages(res) == 1


# ----------------------------------------------------------------------------- store integration

def test_create_job_resume_idempotent_and_persisted(db, real):
    """Expected: create_job_resume tailors to the job, stores job_id/layout/style, is idempotent, makes a new
    version when settings change, and downloads use the persisted layout (one page)."""
    store.import_original(db, real and REAL_RESUME, user_id=ADMIN)
    jid = add_job(db, 1, company="Stripe", title="Junior .NET Developer", description_text=DOTNET_JD)
    v1 = store.create_job_resume(db, jid, "user:alex", user_id=ADMIN)
    v2 = store.create_job_resume(db, jid, "user:alex", user_id=ADMIN)
    assert v1["id"] == v2["id"]
    assert db.scalar("SELECT COUNT(*) FROM resume_versions WHERE job_id=?", (jid,)) == 1
    assert v1["job_id"] == jid and v1["kind"] == "variant" and v1["label"].startswith("Tailored · Stripe")
    assert v1["layout"]["pages"] == 1 and v1["style"]["company_type"] == "big_tech"
    assert v1["target_country"] == "IE" and v1["role_family"] == "dotnet_backend"
    assert page_count(store.export_version(v1, "pdf")) == 1
    assert store.export_version(v1, "docx").startswith(b"PK")
    assert store.download_filename(v1, "pdf").endswith(f"_CV_v{v1['id']}.pdf")
    assert store.job_resume(db, jid, user_id=ADMIN)["id"] == v1["id"]
    settings_mod.set_value(db, "resume_style", dict(settings_mod.DEFAULT_SETTINGS["resume_style"],
                                                    company_type="enterprise"), user_id=ADMIN)
    v3 = store.create_job_resume(db, jid, "user:alex", user_id=ADMIN)
    assert v3["id"] != v1["id"] and v3["style"]["company_type"] == "enterprise"


def test_create_variant_uses_tailor_and_persists_layout(db, files):
    """Expected: generic create_variant is tailored, one page, stores layout/style and stays idempotent."""
    store.import_original(db, files["pdf"], user_id=ADMIN)
    v = store.create_variant(db, "dotnet_backend", "GB", user_id=ADMIN)
    assert v["layout"]["pages"] == 1 and v["style"]["spelling"] == "uk" and v["job_id"] is None
    assert v["structured"]["headings"]["experience"] == "Work Experience"
    assert store.create_variant(db, "dotnet_backend", "GB", user_id=ADMIN)["id"] == v["id"]
    assert page_count(store.export_version(v, "pdf")) == 1


def test_create_job_resume_requires_job(db, files):
    store.import_original(db, files["pdf"], user_id=ADMIN)
    with pytest.raises(LookupError):
        store.create_job_resume(db, 999, "user:alex", user_id=ADMIN)


# ----------------------------------------------------------------------------- optional live AI

@pytest.mark.skipif(os.environ.get("JOBAGENT_LIVE_AI") != "1", reason="live AI check is opt-in")
def test_live_ai_rewrite_dotnet_dublin(db, real):  # pragma: no cover - network
    from jobagent.ai import make_provider
    from jobagent.config import load_config
    s, a = real
    ai = AIService(db, make_provider(load_config()), settings_mod.DEFAULT_SETTINGS["ai"], user_id=ADMIN)
    if not ai.available:
        pytest.skip("no AI provider configured")
    res = tailor(s, a, CAND, dict(NO_AI, ai_rewrite=True), job={"title": "Junior .NET Developer",
                 "description_text": DOTNET_JD, "country": "IE", "employer": "Stripe"}, ai=ai)
    assert ai.run_cost < 0.05 and res.guard_problems == [] and _pages(res) == 1


# ----------------------------------------------------------------------------- 1) master resume edit (E.M.A.)

def test_apply_ema_update_replaces_chatbot_facts(real):
    """Expected: the two old chatbot bullets become the four E.M.A. bullets, tech tag/summary/skills updated,
    no numbers added, input not mutated, idempotent."""
    from jobagent.resume.edits import EMA_BULLETS, apply_ema_update
    s, _ = real
    before = json.dumps(s, sort_keys=True)
    u = apply_ema_update(s)
    assert json.dumps(s, sort_keys=True) == before
    emp = [p for p in u["experience"][0]["projects"] if p["name"].startswith("Empower")][0]
    assert all(b in emp["bullets"] for b in EMA_BULLETS)
    assert not any("GPT-powered support chatbot" in b or "escalation workflow" in b for b in emp["bullets"])
    assert len(emp["bullets"]) == 7 and "Google Gemini API" in emp["tech"] and "OpenAI GPT API" not in emp["tech"]
    assert "E.M.A., a Google Gemini assistant that answers business questions" in u["summary"]
    llm = [i for items in u["skills"].values() for i in items if i.startswith("LLM-Assisted")][0]
    assert llm.startswith("LLM-Assisted Development (Google Gemini, OpenAI GPT, Claude AI")
    assert not re.findall(r"\d", " ".join(EMA_BULLETS))
    assert "Gemini" in u["text"] and apply_ema_update(u) == u


def test_apply_ema_update_not_applicable(synthetic):
    from jobagent.resume.edits import apply_ema_update
    with pytest.raises(ValueError):
        apply_ema_update(synthetic)


def test_create_edited_canonical_moves_canonical_and_feeds_tailoring(db, real):
    """Expected: confirmation required; new 'original' row (manual_edit, parent=old canonical, no file) becomes
    canonical, is audited, and job resumes are built from it with Gemini allowed by the fact guard."""
    from jobagent.resume.edits import apply_ema_update
    old = store.import_original(db, REAL_RESUME, user_id=ADMIN)
    with pytest.raises(PermissionError):
        store.create_edited_canonical(db, apply_ema_update(old["structured"]), "user:alex", "E.M.A.", False, user_id=ADMIN)
    new = store.create_edited_canonical(db, apply_ema_update(old["structured"]), "user:alex",
                                        "E.M.A. chatbot on Google Gemini (user-confirmed)", True, user_id=ADMIN)
    assert new["is_canonical"] == 1 and new["kind"] == "original" and new["method"] == "manual_edit"
    assert new["parent_id"] == old["id"] and new["source_file_id"] is None and new["source_path"] is None
    assert store.get(db, old["id"], user_id=ADMIN)["is_canonical"] == 0 and store.canonical(db, user_id=ADMIN)["id"] == new["id"]
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action='edit_canonical'") == 1
    assert bytes(store.original_file(db, old["id"], user_id=ADMIN)["data"]) == REAL_RESUME.read_bytes()
    jid = add_job(db, 1, company="IBM", title="Software Developer (AI)", description_text=AI_JD)
    v = store.create_job_resume(db, jid, "user:alex", user_id=ADMIN)
    txt = render_text(v["structured"])
    assert "E.M.A." in txt and "GPT-powered support chatbot" not in txt and v["parent_id"] == new["id"]


# ----------------------------------------------------------------------------- 2) MNC company profiles

def test_company_profiles_match_aliases_and_sectors():
    """Expected: employer aliases resolve to the company profile and sector; an explicit setting still wins for
    company type; every requested MNC has a profile with values and keyword boosts."""
    cases = {"Google LLC": ("google", "big_tech"), "Amazon Web Services": ("amazon", "big_tech"),
             "Morgan Stanley Services Group": ("morgan_stanley", "enterprise"),
             "Deloitte USI": ("deloitte", "enterprise"), "KPMG": ("kpmg", "enterprise"),
             "Capgemini Engineering": ("capgemini", "enterprise"), "IBM": ("ibm", "enterprise"),
             "Barclays Bank": ("barclays", "enterprise"), "Atlassian": ("atlassian", "big_tech"),
             "JP Morgan Chase": ("jpmorgan", "enterprise")}
    for emp, (key, ctype) in cases.items():
        st = resolve_style({"company_type": "auto"}, employer=emp)
        assert st["company_profile"]["key"] == key, emp
        assert st["company_type"] == ctype and st["company_type_source"] == "company_profile", emp
    st = resolve_style({"company_type": "startup"}, employer="Google")
    assert st["company_type"] == "startup" and st["company_profile"]["key"] == "google"
    assert resolve_style({}, employer="Eyeo GmbH")["company_profile"] is None   # 'ey' alias must not match
    profs = styles()["company_profiles"]
    for key in ["google", "amazon", "morgan_stanley", "deloitte", "kpmg", "capgemini", "ibm", "barclays",
                "atlassian", "microsoft", "jpmorgan", "goldman_sachs", "accenture", "ey", "pwc", "oracle", "sap",
                "salesforce", "cisco", "adobe", "infosys", "tcs", "wipro", "hcltech", "cognizant", "citi", "hsbc",
                "deutsche_bank", "ubs", "visa", "mastercard", "thoughtworks"]:
        p = profs[key]
        assert p["sector"] in styles()["sectors"] and p["values"] and p["keyword_boosts"] and p["aliases"]
    assert "Customer Obsession" in profs["amazon"]["values"]


def test_company_profile_orders_but_never_adds(real):
    """Expected: a bank profile puts skills before experience while the output stays within the canonical facts
    (no Java although the profile boosts it, no company name/values written into the resume)."""
    from jobagent.resume.edits import apply_ema_update
    s, _ = real
    c = apply_ema_update(s)
    res = tailor(c, None, CAND, NO_AI, job={"title": "Software Engineer", "employer": "Barclays",
                                            "description_text": DOTNET_JD})
    st = res.structured
    assert st["section_order"].index("skills") < st["section_order"].index("experience")
    assert res.guard_problems == [] and not re.search(r"\bJava\b", render_text(st))
    assert "Barclays" not in render_text(st)
    g = tailor(c, None, CAND, NO_AI, job={"title": "Software Engineer", "employer": "Google",
                                          "description_text": DOTNET_JD})
    assert g.structured["section_order"][1] == "experience" and _pages(g) == 1
    assert "Requires visa sponsorship outside India" in g.structured["tagline"]


# ----------------------------------------------------------------------------- 3) JD coverage report

def test_coverage_report_is_honest():
    """Expected: have/partial/missing with evidence; missing skills are suggested, never added to the resume."""
    jd_text = ("Backend Engineer\nRequirements\n• C# and ASP.NET Core Web API\n• Java\n• Kubernetes\n"
               "Nice to have\n• Terraform\n")
    res = tailor(_mini(), None, CAND, NO_AI, job={"title": "Backend Engineer", "description_text": jd_text,
                                                  "country": "IE"})
    cov = {m["skill"]: m for m in res.coverage["must_have"]}
    assert cov["C#"]["status"] == "have" and "C#" in cov["C#"]["evidence"]
    assert cov["Java"]["status"] == "partial" and "related" in cov["Java"]["evidence"]
    assert cov["Kubernetes"]["status"] == "missing" and cov["Kubernetes"]["evidence"] is None
    assert [m["status"] for m in res.coverage["nice_to_have"]] == ["missing"]
    assert "Kubernetes" in res.coverage["missing_keywords"] and 0 < res.coverage["keyword_coverage_pct"] < 100
    sug = " ".join(res.coverage["suggestions"])
    assert "only if you have actually used it" in sug and "Kubernetes" in sug
    assert "Kubernetes" not in render_text(res.structured) and "Terraform" not in render_text(res.structured)
    assert res.style["coverage"] == res.coverage
    assert tailor(_mini(), None, CAND, NO_AI, role_family="dotnet_backend").coverage["keyword_coverage_pct"] is None


# ----------------------------------------------------------------------------- 4) ATS score

def test_ats_score_components_deterministic(real):
    """Expected: six documented components summing to 100 max; identical input -> identical score."""
    from jobagent.resume.ats import ats_score
    s, a = real
    res = tailor(s, a, CAND, NO_AI, job={"title": "Junior .NET Developer", "description_text": DOTNET_JD,
                                         "country": "IE"})
    jd = analyze_jd("Junior .NET Developer", DOTNET_JD)
    lay = dict(res.layout, bullet_max_chars=res.style["bullet_max_chars"])
    a1 = ats_score(res.structured, lay, jd, "Junior .NET Developer", s, DOTNET_JD)
    a2 = ats_score(res.structured, lay, jd, "Junior .NET Developer", s, DOTNET_JD)
    assert a1 == a2
    assert set(a1["components"]) == {"parseability", "keywords", "title", "metrics", "sections", "formatting"}
    assert sum(c["max"] for c in a1["components"].values()) == 100
    assert res.ats["score"] == a1["score"] and res.style["ats"]["score"] == a1["score"]


def test_well_matched_jd_reaches_95(synthetic):
    """Expected: a JD the candidate truly matches scores >= 95 after tailoring, with a truthful headline."""
    jd_text = ("Junior .NET Developer\nRequirements\n• C# and ASP.NET Core\n• SQL Server and Entity "
               "Framework\n• Python\nNice to have\n• Docker and AWS\n")
    res = tailor(synthetic, None, CAND, NO_AI, job={"title": "Junior .NET Developer",
                                                    "description_text": jd_text, "country": "IE"})
    assert res.ats["score"] >= 95, json.dumps(res.ats["components"], indent=1)
    assert res.structured["headline"] == ".NET Developer"             # 'Junior' stripped, nothing invented
    assert res.ats["components"]["parseability"]["score"] == 25 and res.guard_problems == []


def test_unreachable_jd_never_gets_missing_skills(synthetic):
    """Expected: Kubernetes/Go/Rust required -> not added anywhere, score stays < 95, listed as blockers/missing."""
    jd_text = ("Platform Engineer\nRequirements\n• Kubernetes\n• Golang\n• Rust\n• Terraform\n"
               "• C#\n")
    res = tailor(synthetic, None, CAND, NO_AI, job={"title": "Platform Engineer", "description_text": jd_text,
                                                    "country": "GB"})
    text = render_text(res.structured)
    for bad in ("Kubernetes", "Golang", "Rust", "Terraform"):
        assert bad not in text
    assert res.ats["score"] < 95
    missing = {m["keyword"] for m in res.ats["missing_keywords"]}
    assert {"Kubernetes", "Go", "Rust", "Terraform"} <= missing
    assert any("Kubernetes" in b for b in res.ats["blockers"])
    assert "Platform" not in res.structured["headline"] or "targeting" in res.structured["headline"]
    assert res.guard_problems == []


def test_ats_aliases_only_for_held_skills(synthetic):
    """Expected: the JD's wording is added only for skills the candidate holds (alias allowlist passes the guard)."""
    jd_text = ("Backend Developer\nRequirements\n• .NET Core and C#\n• Kubernetes\n"
               "• Microsoft SQL Server\n")
    res = tailor(synthetic, None, CAND, NO_AI, job={"title": "Backend Developer", "description_text": jd_text,
                                                    "country": "IE"})
    skills_text = " ".join(f"{c}: {', '.join(i)}" for c, i in res.structured["skills"].items())
    assert ".NET Core" in skills_text and "Kubernetes" not in skills_text
    assert ".NET Core" in res.style["guard_allowed_text"] and res.guard_problems == []
    assert any("job posting's wording" in c["reason"] for c in res.changes)


@pytest.mark.parametrize("family,country", [("dotnet_backend", "IE"), ("ai_application", "GB"),
                                            ("python_backend", "US"), ("fullstack", "DE")])
def test_round_trip_parse_of_generated_pdf(real, family, country):
    """Expected: every generated PDF parses back with name, email, all sections, dates and single column."""
    from jobagent.resume.ats import ats_score
    s, a = real
    res = tailor(s, a, CAND, NO_AI, role_family=family, country=country)
    sc = ats_score(res.structured, dict(res.layout, bullet_max_chars=res.style["bullet_max_chars"]), None, None, s)
    p = sc["components"]["parseability"]
    assert p["score"] == p["max"], p["details"]


def test_coverage_persisted_with_job_resume(db, files):
    """Expected: the coverage report is stored with the tailored version for the UI."""
    store.import_original(db, files["pdf"], user_id=ADMIN)
    jid = add_job(db, 1, company="Acme", title="Junior .NET Developer", description_text=DOTNET_JD)
    v = store.create_job_resume(db, jid, "user:alex", user_id=ADMIN)
    cov = v["style"]["coverage"]
    assert cov["must_have"] and cov["keyword_coverage_pct"] is not None
    assert {m["status"] for m in cov["must_have"]} <= {"have", "partial", "missing"}
