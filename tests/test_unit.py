"""Unit tests: matching, JD extraction, sponsorship classification, settings validation, location parsing."""
from __future__ import annotations

import copy

import pytest

from factories import DOTNET_JD
from jobagent import jd
from jobagent import settings as settings_mod
from jobagent.matching import score_job
from jobagent.normalize import parse_location
from jobagent.settings import SettingsError
from jobagent.sponsorship import classify

S = settings_mod.DEFAULT_SETTINGS
CAND = S["candidate"]
PROFILE = {"skills": {k: "demonstrated" for k in ["C#", ".NET", "ASP.NET Core", "Web API", "SQL", "Entity Framework",
                                                  "Docker", "REST APIs"]},
           "experience_years": 2.0, "languages": {"english": "C1"}, "work_authorization": {"IN": "citizen"}}


def score(title, text, country="IE", settings=S, remote="onsite"):
    an = jd.analyze(title, text)
    sp = classify(text, country=country, candidate=CAND)
    return score_job({"title": title, "country": country, "remote_type": remote, "description_text": text},
                     an, sp, PROFILE, settings)


def test_senior_title_disqualified():
    """Expected: a 'Senior' title gets a seniority disqualifier and recommendation 'skip'."""
    r = score("Senior Software Engineer", DOTNET_JD)
    assert r["recommendation"] == "skip" and any("senior" in d for d in r["disqualifiers"])


def test_dotnet_junior_ireland_sponsorship_apply():
    """Expected: a junior .NET role in Ireland with confirmed sponsorship scores 'apply' with reasons."""
    r = score("Junior .NET Developer", DOTNET_JD)
    assert r["recommendation"] == "apply" and r["score"] >= 68 and not r["disqualifiers"]
    assert any("sponsorship" in x.lower() for x in r["reasons"])


def test_weights_configurable_change_score():
    """Expected: changing weights changes the total score deterministically."""
    text = "Requirements\n• Rust and Kotlin\n"
    base = score("Software Engineer", text)["score"]
    s2 = copy.deepcopy(S)
    s2["weights"] = {"technical": 100, "experience": 0, "sponsorship": 0, "role": 0, "location_language": 0,
                     "compensation": 0}
    r2 = score("Software Engineer", text, settings=s2)
    assert r2["score"] != base and r2["weights"]["technical"] == 100


def test_low_score_keeps_reasons():
    """Expected: a poor match is not silently dropped: it returns a score, recommendation and explicit gaps/notes."""
    r = score("Software Engineer", "Requirements\n• Rust, Scala and Kotlin\n• Haskell\n")
    assert r["recommendation"] in ("skip", "improve_skills", "review")
    assert r["score"] < 68
    assert any("Required but not established" in g for g in r["gaps"])
    assert r["components"]["technical"] < 0.5


@pytest.mark.parametrize("text,years", [
    ("You have at least 3 years of experience with C#.", 3.0),
    ("2-4 years experience in software development", 2.0),
    ("Minimum of 5+ years of professional experience", 5.0),
    ("Great team, free lunch.", None),
])
def test_experience_years(text, years):
    """Expected: the smallest explicit years-of-experience requirement is extracted (None if absent)."""
    assert jd.experience_years(text)[0] == years


@pytest.mark.parametrize("text,lang,required", [
    ("Fluent German (C1) is required.", "German", True),
    ("Dutch language skills are a plus.", "Dutch", False),
    ("Wir suchen dich! Dein Profil: C#", "German", True),
])
def test_language_requirements(text, lang, required):
    """Expected: spoken-language requirements are detected with the correct required flag."""
    out = {l["language"]: l["required"] for l in jd.language_requirements(text)}
    assert out.get(lang) is required


@pytest.mark.parametrize("text,expect", [
    ("Salary: €50,000 - €70,000 per year", (50000, 70000, "EUR")),
    ("£45k-£55k plus bonus", (45000, 55000, "GBP")),
    ("Competitive salary", None),
])
def test_salary_from_text(text, expect):
    """Expected: salary ranges are parsed with currency; vague text yields None."""
    r = jd.salary_from_text(text)
    assert (None if r is None else (r["min"], r["max"], r["currency"])) == expect


@pytest.mark.parametrize("text,employer,country,cls", [
    ("We offer visa sponsorship for the right candidate.", None, "IE", "confirmed"),
    ("Unfortunately we are unable to offer visa sponsorship.", None, "IE", "not_supported"),
    ("Visa sponsorship is available. Note: we cannot provide sponsorship for contractors.", None, "IE", "unknown"),
    ("A normal job ad.", {"uk_sponsor_status": "listed", "uk_sponsor_routes": "Skilled Worker"}, "GB", "likely"),
    ("A normal job ad.", None, "IE", "unknown"),
])
def test_sponsorship_classify(text, employer, country, cls):
    """Expected: positive -> confirmed, negative -> not_supported, conflicting -> unknown, UK register listed -> likely."""
    r = classify(text, country=country, candidate=CAND, employer=employer)
    assert r["classification"] == cls
    if cls != "unknown" or employer:
        assert r["evidence"]


@pytest.mark.parametrize("key,value", [
    ("nope", 1),
    ("weights", {k: 0 for k in S["weights"]}),
    ("operating_mode", "auto_submit"),
    ("schedule", {"time": "25:00", "days": ["MON"]}),
    ("target_countries", {"tier1": ["XX"]}),
    ("max_applications_per_day", 1000),
    ("sources", {"greenhouse": {"boards": ["../etc/passwd"]}}),
    ("ai", {"model": "gpt 4; rm -rf"}),
])
def test_settings_validate_errors(key, value):
    """Expected: invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError."""
    with pytest.raises(SettingsError):
        settings_mod.validate(key, value)


@pytest.mark.parametrize("raw,countries,city,remote,region", [
    ("Dublin, Ireland", ["IE"], "Dublin", "unknown", None),
    ("Edinburgh, Scotland", ["GB"], "Edinburgh", "unknown", None),
    ("Dubai, UAE", ["AE"], "Dubai", "unknown", None),
    ("Toronto, ON", ["CA"], "Toronto", "unknown", None),
    ("Sydney NSW (Hybrid)", ["AU"], "Sydney", "hybrid", None),
    ("Tokyo, Japan", ["JP"], "Tokyo", "unknown", None),
    ("Remote - Europe", [], None, "remote", "EU_REGION"),
])
def test_parse_location(raw, countries, city, remote, region):
    """Expected: locations map to ISO countries, city, remote type and region."""
    r = parse_location(raw)
    assert (r["countries"], r["city"], r["remote_type"], r["region"]) == (countries, city, remote, region)
