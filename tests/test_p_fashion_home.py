"""P. Fashion industry, the personal home screen (greeting, activity, recommended jobs, courses, applications,
"what's new"), Skills & Prep for any role, industry-aware Top MNCs and Relocation & Sponsorship, and the default
screens every user sees. SYNTHETIC data."""
from __future__ import annotations

import pytest

from conftest import TEST_PASSWORD, extract_csrf, web_factory
from factories import ADMIN, make_user
from jobagent import access, careerkit, mnc, taxonomy
from jobagent import settings as settings_mod
from jobagent.db import dumps
from webdata import insert_job


# =============================================================================== fashion taxonomy

@pytest.mark.parametrize("title,role", [
    ("Fashion Stylist", "fashion_stylist"), ("Personal Shopper", "fashion_stylist"),
    ("Wardrobe Assistant", "fashion_stylist"), ("Image Consultant", "fashion_stylist"),
    ("Senior Visual Merchandiser", "visual_merchandiser"), ("Window Dresser", "visual_merchandiser"),
    ("Fashion Designer", "fashion_designer"), ("Textile Designer", "fashion_designer"),
    ("Pattern Maker", "fashion_designer"), ("Fashion Merchandiser", "fashion_buying"),
    ("Womenswear Buyer", "fashion_buying"), ("Buying Assistant", "fashion_buying"),
    ("Luxury Sales Associate", "fashion_retail"), ("Fashion Consultant", "fashion_retail"),
    ("Fashion Marketing Manager", "fashion_marketing"), ("Garment Production Manager", "fashion_production"),
])
def test_fashion_titles_are_classified(title, role):
    assert taxonomy.classify(title) == ("fashion", role)


@pytest.mark.parametrize("title,expected", [
    ("Hair Stylist", (None, None)), ("Nail Stylist", (None, None)), ("Food Stylist", (None, None)),
    ("Software Engineer", ("technology", "software_engineer")), ("Retail Sales Assistant", ("hospitality", "retail")),
    ("Procurement Buyer", ("operations", "procurement")), ("Graphic Designer", ("design", "graphic_designer")),
    ("Client Advisor", (None, None)),                         # banks use this title too: not fashion by itself
])
def test_fashion_does_not_steal_other_titles(title, expected):
    assert taxonomy.classify(title) == expected


def test_fashion_industry_is_offered_and_searchable():
    ind = taxonomy.BY_ID["fashion"]
    assert ind.label.startswith("Fashion") and len(ind.roles) >= 6
    assert all(r.query for r in ind.roles)                      # every role has a job-board search query
    assert taxonomy.classify("Merch role", ["Fashion & Apparel"]) == ("fashion", None)


# =============================================================================== career kit

def test_every_role_has_skills_and_official_courses():
    assert set(careerkit.KIT) == set(taxonomy.ROLES)
    for rid, (skills, courses) in careerkit.KIT.items():
        assert len(skills) >= 4, rid
        assert courses, rid
        for c in courses:
            assert c.url.startswith("https://") and c.provider and c.level in ("start", "experienced"), (rid, c)


def test_courses_for_a_fresher_put_starter_courses_first():
    career = {"industries": ["fashion"], "roles": ["fashion_stylist", "visual_merchandiser"], "experience": "entry"}
    cs = careerkit.courses_for(career, limit=6)
    names = [c["name"] for c in cs]
    assert any("London College of Fashion" in c["provider"] for c in cs)
    assert any("NIFT" in c["provider"] for c in cs)
    assert len(names) == len(set(names))                       # de-duplicated across roles
    shared = next(c for c in cs if "London College of Fashion" in c["provider"])
    assert set(shared["roles"]) == {"Fashion Stylist / Image Consultant", "Visual Merchandiser"}
    # someone early in their career sees experience-gated certifications last
    pm = careerkit.courses_for({"industries": ["business"], "roles": ["project_manager"], "experience": "entry"})
    levels = [c["level"] for c in pm]
    assert levels == sorted(levels, key=lambda lv: lv == "experienced")


def test_courses_fall_back_to_industry_when_no_role():
    cs = careerkit.courses_for({"industries": ["healthcare"], "roles": []})
    assert cs and any("Heart" in c["provider"] for c in cs)
    assert careerkit.courses_for({}) == []


def test_skill_plan_checks_resume_and_job_gaps(db):
    uid = make_user(db, "dia")
    settings_mod.set_value(db, "career", {"industries": ["fashion"], "roles": ["fashion_stylist"],
                                          "experience": "entry"}, "x", user_id=uid)
    with db.transaction():
        db.run("INSERT INTO resume_versions(label, kind, structured_json, method, created_at, is_canonical, user_id, "
               "status) VALUES ('cv','original',?, 'rules', '2026-11-01T00:00:00+00:00', 1, ?, 'approved')",
               (dumps({"skills": ["Mood boards", "Client consultation", "Canva"]}), uid))
        jid = insert_job(db, title="Fashion Stylist", user_id=uid)
        db.run("INSERT INTO job_scores(job_id, user_id, score, recommendation, breakdown_json, details_json, "
               "weights_json, created_at) VALUES (?,?,?,?, '{}', ?, '{}', '2026-11-20T00:00:00+00:00')",
               (jid, uid, 61.0, "review", dumps({"must_have": [{"skill": "Adobe Photoshop", "credit": 0.0},
                                                             {"skill": "Canva", "credit": 1.0}]})))
    plan = careerkit.skill_plan(db, uid)
    assert plan["career_set"] and plan["has_resume"] and plan["early"]
    skills = {s["skill"]: s["have"] for s in plan["roles"][0]["skills"]}
    assert skills["Mood boards"] and skills["Client consultation"]
    assert not skills["Trend forecasting"]
    assert plan["gaps"]["skills"] == [{"skill": "Adobe Photoshop", "jobs": 1}]
    assert plan["gaps"]["avg_score"] == 61.0
    assert plan["courses"]


# =============================================================================== default screens

def test_default_screens_for_users(db):
    uid = make_user(db, "dia")
    assert access.resolve(db, uid) == {"hidden": ["/answers", "/runs", "/audit"], "source": "automatic"}
    settings_mod.set_value(db, "career", {"industries": ["fashion"], "roles": ["fashion_stylist"]}, "x", user_id=uid)
    cand = settings_mod.get(db, "candidate", uid)
    settings_mod.set_value(db, "candidate", {**cand, "requires_sponsorship_outside": False}, "x", user_id=uid)
    r = access.resolve(db, uid)
    assert r["hidden"] == ["/answers", "/runs", "/audit"]      # Top MNCs and Relocation stay visible
    access.set_user(db, uid, "custom", [p for p in access.SCREEN_PATHS if p != "/audit"], "admin")
    assert access.resolve(db, uid)["hidden"] == ["/audit"]     # the administrator can grant extra screens
    assert access.resolve(db, ADMIN)["hidden"] == []


# =============================================================================== web

@pytest.fixture
def fashion_site(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    d = w.db()
    dia = make_user(d, "diya", full_name="diya soni")
    settings_mod.set_value(d, "career", {"industries": ["fashion"], "roles": ["fashion_stylist", "visual_merchandiser"],
                                         "experience": "entry"}, "x", user_id=dia)
    settings_mod.set_value(d, "target_countries", {"tier1": ["IN"], "tier2": [], "tier3": []}, "x", user_id=dia)
    with d.transaction():
        style = insert_job(d, title="Fashion Stylist", company="Zara", country="IN", city="Mumbai", user_id=dia,
                           score=78, rec="apply", sponsorship="confirmed")
        vm = insert_job(d, title="Visual Merchandiser", company="H&M", country="IN", city="Mumbai", user_id=dia)
        dev = insert_job(d, title="Backend Software Engineer", company="Stripe", country="IN", user_id=dia,
                         score=90, rec="apply", sponsorship="confirmed")
        uk = insert_job(d, title="Fashion Stylist", company="Selfridges", country="GB", city="London", user_id=dia,
                        sponsorship="likely")
        applied = insert_job(d, title="Personal Shopper", company="Myntra", country="IN", user_id=dia,
                             app_status="submitted")
        for jid, title in ((style, "Fashion Stylist"), (vm, "Visual Merchandiser"), (dev, "Backend Software Engineer"),
                           (uk, "Fashion Stylist"), (applied, "Personal Shopper")):
            ind, role = taxonomy.classify(title)
            d.run("UPDATE jobs SET industry=?, role_key=? WHERE id=?", (ind, role, jid))
    d.close()
    w.ids = {"dia": dia, "style": style, "vm": vm, "dev": dev, "uk": uk, "applied": applied}
    return w


def test_user_home_is_personal(fashion_site):
    w = fashion_site
    assert w.login("diya", TEST_PASSWORD).status_code == 303
    home = w.client.get("/").text
    assert 'data-greet="Diya"' in home and "Welcome back, Diya" in home
    assert "Recommended for your roles" in home
    rec = home.split("Recommended for your roles")[1].split("Your applications")[0]
    assert "Fashion Stylist" in rec and "Visual Merchandiser" in rec
    assert "Backend Software Engineer" not in rec                # outside her roles
    assert "Personal Shopper" not in rec                         # already applied: listed under applications
    apps_part = home.split("Your applications")[1]
    assert "Personal Shopper" in apps_part and "Myntra" in apps_part
    assert "Courses to strengthen your profile" in home and "London College of Fashion" in home
    assert "What&#39;s new in JobAgent" in home or "What's new in JobAgent" in home
    assert "Agent runs" not in home and "Jobs tracked" not in home   # the admin dashboard is not shown
    # nav: the user's default screens only
    for p in ("/jobs", "/mnc", "/applications", "/recruiters", "/companion", "/sponsorship", "/studio", "/prep",
              "/welcome", "/support", "/settings"):
        assert f'href="{p}"' in home, p
    for p in ("/runs", "/answers", "/audit", "/admin/users"):
        assert f'href="{p}"' not in home, p
    # dismissing "what's new" keeps it away
    r = w.client.post("/whats-new/seen", data={"csrf": extract_csrf(home)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "What's new in JobAgent" not in w.client.get("/").text
    assert w.client.post("/whats-new/seen", data={"csrf": "bad"}).status_code in (400, 403)


def test_admin_keeps_the_dashboard(fashion_site):
    w = fashion_site
    assert w.login().status_code == 303
    page = w.client.get("/").text
    assert "Jobs tracked" in page and "Recommended for your roles" not in page
    assert 'href="/runs"' in page and 'href="/audit"' in page


def test_sponsorship_follows_the_users_profile(fashion_site):
    w = fashion_site
    assert w.login("diya", TEST_PASSWORD).status_code == 303
    page = w.client.get("/sponsorship").text
    assert "Relocation &amp; Sponsorship" in page or "Relocation & Sponsorship" in page
    table = page.split('id="jobs-h"')[1]
    assert "Zara" in table                                       # fashion, India (her target)
    assert "Stripe" not in table                                 # tech: not her industry
    assert "Selfridges" not in table                             # UK: not a target country


def test_top_mncs_follow_the_users_industry(fashion_site):
    w = fashion_site
    assert w.login("diya", TEST_PASSWORD).status_code == 303
    page = w.client.get("/mnc").text
    assert "Inditex (Zara)" in page and "H&amp;M Group" in page
    assert "Goldman Sachs" not in page and "Infosys" not in page
    assert w.client.get("/mnc?industry=finance").status_code == 200
    assert "Goldman Sachs" not in w.client.get("/mnc?industry=finance").text   # users can't browse other industries
    w.client.cookies.clear()
    assert w.login().status_code == 303
    assert "Goldman Sachs" in w.client.get("/mnc").text            # administrator: everything
    fashion = w.client.get("/mnc?industry=fashion").text
    assert "Kering" in fashion and "Goldman Sachs" not in fashion


def test_prep_shows_skills_and_courses_for_any_role(fashion_site):
    w = fashion_site
    assert w.login("diya", TEST_PASSWORD).status_code == 303
    page = w.client.get("/prep").text
    assert "Skills for your roles" in page and "Trend forecasting" in page and "Planograms" in page
    assert "Recommended courses &amp; certifications" in page or "Recommended courses & certifications" in page
    assert "Data structures &amp; algorithms" not in page


def test_mnc_catalogue_covers_every_industry():
    covered = {i for c in mnc.catalog() for i in c.get("industries") or []}
    missing = {i.id for i in taxonomy.INDUSTRIES} - covered
    assert not missing, missing
    for c in mnc.catalog():
        assert c["careers_url"].startswith("https://") and c.get("industries"), c["key"]
    assert mnc.match_employer("Zara India")["key"] == "inditex"
    assert mnc.match_employer("Apollo Tyres") is None              # no bare "apollo" alias
