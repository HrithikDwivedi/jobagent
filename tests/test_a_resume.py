"""A. Resume handling (synthetic files + read-only real resume)."""
from __future__ import annotations

import copy
import hashlib
from datetime import date
from pathlib import Path

import pytest

from factories import ADMIN, pdf_bytes
from jobagent.resume import store
from jobagent.resume.analysis import analyze
from jobagent.resume.parser import ResumeError, discover_resume, parse, sha256_file
from jobagent.resume.variants import build_variant, verify_no_new_facts


def _codes(fn):
    with pytest.raises(ResumeError) as ei:
        fn()
    return ei.value


def test_correct_pdf_discovery_file(files):
    """Expected: an explicit PDF path is returned unchanged by discover_resume."""
    assert discover_resume(files["pdf"]) == files["pdf"]


def test_correct_pdf_discovery_directory(files):
    """Expected: a folder holding exactly one resume resolves to that file."""
    assert discover_resume(files["single_dir"]) == files["pdf"]


def test_missing_resume_directory(files):
    """Expected: a path inside a missing folder raises not_found mentioning the missing folder."""
    e = _codes(lambda: discover_resume(files["missing_dir"]))
    assert e.code == "not_found" and "folder missing" in e.message


def test_resume_not_configured():
    """Expected: no configured path raises not_configured."""
    assert _codes(lambda: discover_resume(None)).code == "not_configured"


def test_incorrect_filename_suggests_nearby(files):
    """Expected: a misspelt filename raises not_found and lists the nearby resume as a suggestion."""
    wrong = files["single_dir"] / "Alex_Exmaple_Resume.pdf"
    e = _codes(lambda: discover_resume(wrong))
    assert e.code == "not_found"
    assert any(c.endswith("Alex_Example_Resume.pdf") for c in e.candidates)
    assert "Alex_Example_Resume.pdf" in e.message


def test_multiple_candidate_resumes(files):
    """Expected: a folder with several PDFs raises multiple_candidates listing every candidate (never guesses)."""
    e = _codes(lambda: discover_resume(files["multi_dir"]))
    assert e.code == "multiple_candidates"
    assert sorted(Path(c).name for c in e.candidates) == ["resume_a.pdf", "resume_b.pdf"]


def test_corrupted_pdf(files):
    """Expected: garbage bytes with a .pdf extension raise code 'corrupted'."""
    assert _codes(lambda: parse(files["corrupted"])).code == "corrupted"


def test_password_protected_pdf(files):
    """Expected: an AES-256 encrypted PDF raises code 'password_protected'."""
    assert _codes(lambda: parse(files["encrypted"])).code == "password_protected"


def test_scanned_image_only_pdf(files):
    """Expected: an image-only PDF raises 'scanned_no_text' (no OCR, no guessing)."""
    assert _codes(lambda: parse(files["image_only"])).code == "scanned_no_text"


def test_empty_pdf(files):
    """Expected: a PDF with one blank page raises code 'empty'."""
    assert _codes(lambda: parse(files["blank"])).code == "empty"


def test_unsupported_format(files):
    """Expected: a .png resume raises 'unsupported_format' from discovery and from import."""
    assert _codes(lambda: discover_resume(files["png"])).code == "unsupported_format"


def test_unsupported_format_import_bytes(db):
    """Expected: import_bytes refuses a .png upload with 'unsupported_format'."""
    assert _codes(lambda: store.import_bytes(db, b"\x89PNG....", "me.png", user_id=ADMIN)).code == "unsupported_format"


def test_docx_resume_parses(files):
    """Expected: a .docx resume parses into experience roles with dates and bullets."""
    s = parse(files["docx"])
    assert s["experience"] and s["experience"][0]["start"] == "Jan 2023"
    assert "C#" in s["detected_skills"]


def test_synthetic_pdf_structure(files):
    """Expected: the synthetic PDF yields name, email, 2 dated roles, skills and education."""
    s = parse(files["pdf"])
    assert s["name"] == "Alex Example"
    assert s["contact"]["email"] == "alex.example@example.com"
    assert [r["start"] for r in s["experience"]] == ["Jan 2023", "Jun 2022"]
    assert s["skills"]["Languages"][0] == "C#"
    assert s["education"][0]["end_year"] == "2022"


BASE = {
    "summary": "Engineer with 2+ years of experience.",
    "contact": {"email": "a@example.com", "linkedin": "linkedin.com/in/a"},
    "skills": {"Languages": ["C#", "Python"]},
    "experience": [{"title": "Engineer", "company": "X", "start": "Jan 2023", "end": "Jan 2025",
                    "projects": [], "bullets": ["Built C# APIs for 10 teams using Python."]}],
    "projects": [], "education": [], "raw_headings": {}, "layout": {}, "text": "",
}


def _msgs(s):
    return [i["message"] for i in analyze(s, today=date(2026, 11, 1))["issues"]]


def test_missing_experience_dates_flagged():
    """Expected: a role without a parseable start date is flagged as a consistency issue."""
    s = copy.deepcopy(BASE)
    s["experience"][0]["start"] = ""
    assert any("no parseable start date" in m for m in _msgs(s))


def test_duplicate_skills_flagged():
    """Expected: a skill listed twice (case-insensitive, across categories) is flagged."""
    s = copy.deepcopy(BASE)
    s["skills"] = {"Languages": ["C#", "Python"], "Backend": ["c#"]}
    assert any("listed 2 times" in m for m in _msgs(s))


@pytest.mark.parametrize("case,start,end,summary,expect", [
    ("end_before_start", "Jan 2024", "Jan 2023", "Engineer.", "ends before it starts"),
    ("future_start", "Jan 2030", "Present", "Engineer.", "starts in the future"),
    ("summary_overclaims", "Jan 2024", "Jan 2025", "Engineer with 10+ years of experience.", "Summary claims 10+"),
])
def test_inconsistent_dates_flagged(case, start, end, summary, expect):
    """Expected: end-before-start, future start and summary-vs-dated-experience mismatches are flagged high."""
    s = copy.deepcopy(BASE)
    s["experience"][0].update(start=start, end=end)
    s["summary"] = summary
    issues = analyze(s, today=date(2026, 11, 1))["issues"]
    hit = [i for i in issues if expect in i["message"]]
    assert hit and hit[0]["severity"] == "high"


def test_variant_fact_guard_rejects_new_numbers_and_skills(files):
    """Expected: verify_no_new_facts reports a new number and a new skill injected into a variant."""
    canonical = parse(files["pdf"])
    variant, _ = build_variant(canonical, "dotnet_backend", "IE", {"current_location": "Mumbai, India"})
    assert verify_no_new_facts(canonical, variant) == []
    variant["summary"] += " Led a team of 37 engineers running Kubernetes clusters."
    problems = verify_no_new_facts(canonical, variant)
    assert any("'37'" in p for p in problems)
    assert any("Kubernetes" in p for p in problems)


def test_variant_generation_failure_is_reported(db, files, monkeypatch):
    """Expected: create_variant raises ResumeError('fact_guard') and stores nothing when the builder adds facts."""
    store.import_original(db, files["pdf"], user_id=ADMIN)
    import jobagent.resume.store as st

    # create_variant now goes through the tailoring pipeline (tailor()); inject facts there.
    real = st.tailor

    def bad(*a, **k):
        res = real(*a, **k)
        res.structured["summary"] = "Ten years of Kubernetes and 99 certifications."
        return res
    monkeypatch.setattr(st, "tailor", bad)
    e = _codes(lambda: store.create_variant(db, "dotnet_backend", "IE", user_id=ADMIN))
    assert e.code == "fact_guard"
    assert db.scalar("SELECT COUNT(*) FROM resume_versions WHERE kind='variant'") == 0


def test_variant_summary_edit_guard(db, files):
    """Expected: a user summary edit adding an unverified number is rejected with fact_guard."""
    store.import_original(db, files["pdf"], user_id=ADMIN)
    v = store.create_variant(db, "dotnet_backend", "IE", user_id=ADMIN)
    e = _codes(lambda: store.update_variant_summary(db, v["id"], "Engineer with 15 years of Go.", "user:alex", user_id=ADMIN))
    assert e.code == "fact_guard"


def test_original_synthetic_file_preserved_and_idempotent(db, files):
    """Expected: importing leaves the file's sha256 unchanged and re-import returns the same version (by content)."""
    before = sha256_file(files["pdf"])
    a = store.import_original(db, files["pdf"], user_id=ADMIN)
    b = store.import_original(db, files["pdf"], user_id=ADMIN)
    assert sha256_file(files["pdf"]) == before
    assert a["id"] == b["id"]
    assert db.scalar("SELECT COUNT(*) FROM resume_versions WHERE kind='original'") == 1
    assert store.original_file(db, a["id"], user_id=ADMIN)["sha256"] == before


def test_real_resume_preserved(db, real_resume):
    """Expected: parsing and importing the REAL resume never modifies it (sha256 unchanged) and is idempotent."""
    before = hashlib.sha256(real_resume.read_bytes()).hexdigest()
    s = parse(real_resume)
    assert s["experience"], "real resume should contain dated experience"
    a = store.import_original(db, real_resume, user_id=ADMIN)
    b = store.import_original(db, real_resume, user_id=ADMIN)
    assert a["id"] == b["id"]
    assert hashlib.sha256(real_resume.read_bytes()).hexdigest() == before
    assert bytes(store.original_file(db, a["id"], user_id=ADMIN)["data"]) == real_resume.read_bytes()


def test_import_bytes_strips_path_traversal(db):
    """Expected: a '../../x.pdf' upload is stored under its basename 'x.pdf' only."""
    v = store.import_bytes(db, pdf_bytes(), "../../x.pdf", user_id=ADMIN)
    f = store.original_file(db, v["id"], user_id=ADMIN)
    assert f["original_name"] == "x.pdf"
    assert v["structured"]["source_file"] == "x.pdf"


def test_import_bytes_wrong_magic(db):
    """Expected: a .pdf whose bytes are not a PDF is rejected as 'corrupted' before parsing."""
    assert _codes(lambda: store.import_bytes(db, b"MZ\x90\x00 exe bytes", "cv.pdf", user_id=ADMIN)).code == "corrupted"
    assert _codes(lambda: store.import_bytes(db, b"%PDF fake", "cv.docx", user_id=ADMIN)).code == "corrupted"


def test_import_bytes_too_large(db):
    """Expected: uploads over 5 MB are rejected with 'too_large'."""
    big = b"%PDF-1.4\n" + b"0" * (store.MAX_UPLOAD_BYTES + 1)
    assert _codes(lambda: store.import_bytes(db, big, "cv.pdf", user_id=ADMIN)).code == "too_large"


def test_import_bytes_empty(db):
    """Expected: an empty upload is rejected with 'empty'."""
    assert _codes(lambda: store.import_bytes(db, b"", "cv.pdf", user_id=ADMIN)).code == "empty"
