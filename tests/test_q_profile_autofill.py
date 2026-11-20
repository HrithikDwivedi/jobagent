"""Q. Application details from the resume: the Companion/auto-apply recognise every way forms ask for a name, empty
details are filled from the resume on upload (never overwriting the user's own), and the home pop-up asks for what
the resume did not contain. SYNTHETIC data."""
from __future__ import annotations

import pytest

from conftest import TEST_PASSWORD, extract_csrf, web_factory
from factories import build_pdf, make_user
from jobagent import profile
from jobagent import settings as settings_mod
from jobagent.autoapply.matcher import Field, Matcher

RESUME = {"name": "ASHA RAO", "contact": {"email": "asha.rao@example.com", "phone": "+91 98765 43210",
                                          "linkedin": "linkedin.com/in/asha-rao", "github": "github.com/asharao",
                                          "location": "Mumbai, India"}}


@pytest.mark.parametrize("label", ["What is your name?", "Please provide your name", "First and last name",
                                   "Applicant name", "Your name", "Name", "Full name", "Candidate's full name",
                                   "Enter your full name", "Name and surname"])
def test_every_way_of_asking_for_the_name(label):
    d = Matcher({"full_name": "Asha Rao"}, {"country": "GB"}).decide(Field("k", label, "text", required=True))
    assert (d.action, d.value) == ("fill", "Asha Rao"), label


@pytest.mark.parametrize("label", ["Company name", "Name of your referee", "Reference name",
                                   "Emergency contact name", "Manager's name", "School name"])
def test_other_peoples_names_are_not_filled_with_yours(label):
    d = Matcher({"full_name": "Asha Rao"}, {"country": "GB"}).decide(Field("k", label, "text", required=True))
    assert d.value != "Asha Rao", label


def test_split_names_still_work():
    m = Matcher({"full_name": "Asha Devi Rao"}, {})
    assert m.decide(Field("a", "First name", "text")).value == "Asha"
    assert m.decide(Field("b", "Last name", "text")).value == "Rao"


def test_resume_fills_gaps_but_settings_win():
    m = Matcher({}, {"country": "GB"}, [], RESUME)
    assert m.decide(Field("a", "What is your name?", "text")).value == "Asha Rao"      # ALL CAPS tidied
    assert m.decide(Field("b", "Email address", "email")).value == "asha.rao@example.com"
    assert m.decide(Field("c", "LinkedIn profile", "text")).value == "https://www.linkedin.com/in/asha-rao"
    m2 = Matcher({"full_name": "Asha R.", "email": "own@example.com"}, {}, [], RESUME)
    assert m2.decide(Field("a", "Full name", "text")).value == "Asha R."
    assert m2.decide(Field("b", "Email", "email")).value == "own@example.com"


def test_from_resume_rejects_odd_values():
    assert profile.from_resume({"name": "jobs@example.com | 2024", "contact": {"email": "not-an-email",
                                                                               "phone": "call me"}}) == {}
    assert profile.from_resume(None) == {}


def test_upload_fills_empty_details_only(db, tmp_path):
    uid = make_user(db, "asha", full_name="Asha Own Name")
    cand = settings_mod.get(db, "candidate", uid)
    settings_mod.set_value(db, "candidate", {**cand, "full_name": "Asha Own Name"}, "x", user_id=uid)
    filled = profile.fill_from_resume(db, uid, RESUME, "x")
    assert set(filled) == {"email", "phone", "linkedin_url", "github_url", "current_location"}
    c = settings_mod.get(db, "candidate", uid)
    assert c["full_name"] == "Asha Own Name"                                 # never overwritten
    assert c["email"] == "asha.rao@example.com" and c["current_location"] == "Mumbai, India"
    assert profile.fill_from_resume(db, uid, RESUME, "x") == []               # second upload: nothing to do


def test_missing_prompt_save_and_snooze(db):
    uid = make_user(db, "asha")
    profile.fill_from_resume(db, uid, RESUME, "x")
    keys = [f["key"] for f in profile.missing(db, uid)]
    assert keys == ["notice_period", "expected_salary"]
    p = profile.prompt(db, uid)
    assert p and p["currency"] == "INR"                                      # lives in Mumbai
    profile.save_quick(db, uid, {"notice_period": "30 days", "expected_amount": "6,00,000",
                                 "expected_currency": "INR", "expected_period": "year"}, "x")
    assert profile.missing(db, uid) == [] and profile.prompt(db, uid) is None
    c = settings_mod.get(db, "candidate", uid)
    assert c["expected_salary"]["amount"] == 600000 and c["notice_period"] == "30 days"
    with pytest.raises(settings_mod.SettingsError):
        profile.save_quick(db, uid, {"expected_amount": "lots"}, "x")
    other = make_user(db, "ben")
    assert profile.prompt(db, other)
    profile.snooze(db, other, "x")
    assert profile.prompt(db, other) is None


def test_upload_and_home_popup_end_to_end(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    d = w.db()
    make_user(d, "asha", full_name="")
    d.close()
    assert w.login("asha", TEST_PASSWORD).status_code == 303
    home = w.client.get("/").text
    assert "Finish your application details" not in home                     # no resume yet: nothing to ask
    tok = extract_csrf(w.client.get("/studio").text)
    pdf = build_pdf(tmp_path / "cv.pdf")
    r = w.client.post("/studio/upload", data={"csrf": tok},
                      files={"resume": ("cv.pdf", pdf.read_bytes(), "application/pdf")}, follow_redirects=True)
    assert "Resume imported." in r.text and "filled from it" in r.text
    # dismiss "what's new" so the pop-up can show
    w.client.post("/whats-new/seen", data={"csrf": tok})
    home = w.client.get("/").text
    assert "Finish your application details" in home and 'name="notice_period"' in home
    assert 'name="email"' not in home.split("Finish your application details")[1]   # came from the resume
    r = w.client.post("/profile/quick", data={"csrf": tok, "notice_period": "Immediately", "expected_amount": "50000",
                                              "expected_currency": "GBP", "expected_period": "year"},
                      follow_redirects=True)
    assert "Saved." in r.text
    assert "Finish your application details" not in w.client.get("/").text
    r = w.client.post("/profile/quick", data={"csrf": tok}, follow_redirects=True)
    assert "Fill in at least one detail" in r.text
    assert w.client.post("/profile/later", data={"csrf": "bad"}).status_code in (400, 403)


def test_admin_overview_lists_recent_errors(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    from jobagent.db import audit
    d = w.db()
    audit(d, actor="system", entity="error", entity_id="3ec02ea1", action="unhandled_exception",
          detail={"method": "POST", "path": "/jobs/3032/apply", "user": "asha",
                  "error": "KeyError: 'x'", "trace_tail": "Traceback ...\nKeyError: 'x'"})
    make_user(d, "asha")
    d.close()
    assert w.login().status_code == 303
    page = w.client.get("/").text
    assert "Recent errors" in page and "3ec02ea1" in page and "/jobs/3032/apply" in page and "KeyError" in page
    w.client.cookies.clear()
    assert w.login("asha", TEST_PASSWORD).status_code == 303
    assert "3ec02ea1" not in w.client.get("/").text                        # users never see errors
