"""First-run questions for a new user, and the settings they turn into.

Step 1  resume (optional)              -> Resume Studio import; suggests industries/roles from the job titles in it
Step 2  industries (required, max 3)   -> career.industries
Step 3  roles (required, max 3)        -> career.roles / career.custom_roles (roles typed in the user's own words)
Step 4  experience + job type          -> career.experience, career.employment_types
Step 5  where (required)               -> target countries, work arrangements, visa sponsorship, relocation,
                                          current location, minimum salary
finish()                               -> derived settings (preferred role titles, excluded title words for the
                                          level, India-remote flag, work authorisation) + users.onboarded_at

Every step saves immediately (the user can stop and continue later) and every value is validated by settings.py."""
from __future__ import annotations

from . import settings as settings_mod
from . import taxonomy
from .db import Database, audit, iso
from .settings import COUNTRY_NAMES, SettingsError

STEPS = (
    (1, "Resume", "Optional"),
    (2, "Industry", "Up to 3"),
    (3, "Roles", "Up to 3"),
    (4, "Experience", "Level and job type"),
    (5, "Location", "Countries and visa"),
)
MAX_COUNTRIES = 8
SPONSOR_CHOICES = {"no": "No, I can already work in all of them",
                   "some": "In some of them",
                   "yes": "Yes, I need sponsorship in all of them"}


class OnboardingError(Exception):
    pass


def career(db: Database, user_id: int) -> dict:
    return dict(settings_mod.get(db, "career", user_id) or {})


def _save_career(db: Database, user_id: int, actor: str, **changes) -> dict:
    c = career(db, user_id)
    c.update(changes)
    try:
        return settings_mod.set_value(db, "career", c, actor, user_id=user_id)
    except SettingsError as e:
        raise OnboardingError(str(e)) from None


def save_industries(db: Database, user_id: int, actor: str, industries: list[str]) -> dict:
    picks = [i for i in dict.fromkeys(i.strip() for i in industries if i and i.strip())]
    if not picks:
        raise OnboardingError("Choose at least one industry.")
    if len(picks) > settings_mod.MAX_CAREER_PICKS:
        raise OnboardingError(f"Choose at most {settings_mod.MAX_CAREER_PICKS} industries.")
    c = career(db, user_id)
    # roles outside the new industries are dropped (the user is asked for roles next anyway)
    roles = [r for r in c.get("roles") or [] if r in taxonomy.ROLES and taxonomy.ROLES[r][0] in picks]
    return _save_career(db, user_id, actor, industries=picks, roles=roles)


def save_roles(db: Database, user_id: int, actor: str, roles: list[str], custom: list[str]) -> dict:
    picks = [r for r in dict.fromkeys(r.strip() for r in roles if r and r.strip())]
    typed = [" ".join(str(c).split()) for c in custom if c and str(c).strip()]
    if not picks and not typed:
        raise OnboardingError("Choose at least one role (or type the role you want).")
    if not career(db, user_id).get("industries"):
        raise OnboardingError("Choose your industries first.")
    return _save_career(db, user_id, actor, roles=picks, custom_roles=typed)


def save_experience(db: Database, user_id: int, actor: str, level: str, types: list[str]) -> dict:
    if level not in taxonomy.EXPERIENCE:
        raise OnboardingError("Choose your experience level.")
    types = [t for t in dict.fromkeys(types) if t]
    if not types:
        raise OnboardingError("Choose at least one type of job.")
    return _save_career(db, user_id, actor, experience=level, employment_types=types)


def save_location(db: Database, user_id: int, actor: str, *, countries: list[str], arrangements: list[str],
                  sponsorship: str, authorised: list[str], relocate: bool, current_location: str,
                  min_salary: str, currency: str) -> None:
    countries = [c.upper() for c in dict.fromkeys(countries) if c]
    if not countries:
        raise OnboardingError("Choose at least one country where you want to work.")
    if len(countries) > MAX_COUNTRIES:
        raise OnboardingError(f"Choose at most {MAX_COUNTRIES} countries.")
    bad = [c for c in countries if c not in COUNTRY_NAMES]
    if bad:
        raise OnboardingError("Unknown country.")
    arrangements = [a for a in dict.fromkeys(arrangements) if a]
    if not arrangements:
        raise OnboardingError("Choose at least one way of working (on-site, hybrid or remote).")
    if sponsorship not in SPONSOR_CHOICES:
        raise OnboardingError("Tell us whether you need visa sponsorship.")
    if sponsorship == "no":
        allowed = countries
    elif sponsorship == "some":
        allowed = [c for c in dict.fromkeys(a.upper() for a in authorised) if c in countries]
        if not allowed:
            raise OnboardingError("Tick the countries where you can already work.")
    else:
        allowed = []
    salary = 0
    if (min_salary or "").strip():
        try:
            salary = int(float(min_salary.replace(",", "").strip()))
        except ValueError:
            raise OnboardingError("Minimum salary must be a number (or leave it empty).") from None
    cand = dict(settings_mod.get(db, "candidate", user_id) or {})
    wa = {c: v for c, v in (cand.get("work_authorization") or {}).items() if c not in countries}
    wa.update({c: (cand.get("work_authorization") or {}).get(c, "work_permit") for c in allowed})
    cand.update({"work_authorization": wa, "requires_sponsorship_outside": sponsorship != "no",
                 "willing_to_relocate": bool(relocate)})
    loc = " ".join((current_location or "").split())[:120]
    if loc:
        cand["current_location"] = loc
    try:
        settings_mod.set_value(db, "target_countries", {"tier1": countries, "tier2": [], "tier3": []}, actor,
                               user_id=user_id)
        settings_mod.set_value(db, "include_india_remote", "IN" in countries, actor, user_id=user_id)
        settings_mod.set_value(db, "work_arrangements", arrangements, actor, user_id=user_id)
        settings_mod.set_value(db, "candidate", cand, actor, user_id=user_id)
        settings_mod.set_value(db, "min_salary", salary, actor, user_id=user_id)
        if currency:
            settings_mod.set_value(db, "currency", currency, actor, user_id=user_id)
        # a user who needs no sponsorship should not have jobs filtered on sponsorship evidence
        settings_mod.set_value(db, "sponsorship_policy", "any" if sponsorship == "no" else "exclude_not_supported",
                               actor, user_id=user_id)
    except SettingsError as e:
        raise OnboardingError(str(e)) from None
    mark_location_saved(db, user_id, actor)


def missing(db: Database, user_id: int) -> list[int]:
    """Steps (2-5) that still need an answer."""
    c = career(db, user_id)
    out = []
    if not c.get("industries"):
        out.append(2)
    if not (c.get("roles") or c.get("custom_roles")):
        out.append(3)
    if not c.get("experience"):
        out.append(4)
    location_done = c.get("onboarded_at") or db.scalar(
        "SELECT 1 FROM audit_events WHERE entity='onboarding' AND entity_id=? AND action='location_saved'",
        (str(user_id),))
    if not location_done:
        out.append(5)
    return out


def finish(db: Database, user_id: int, actor: str, account: dict | None = None) -> dict:
    """Derive the matching settings from the answers and mark the account onboarded."""
    todo = missing(db, user_id)
    if todo:
        raise OnboardingError(f"Please answer step {todo[0]} first.")
    c = career(db, user_id)
    terms = taxonomy.preferred_role_terms(c)
    try:
        if terms:
            settings_mod.set_value(db, "preferred_roles", terms, actor, user_id=user_id)
        settings_mod.set_value(db, "excluded_title_terms", taxonomy.excluded_terms_for(c.get("experience"), c),
                               actor, user_id=user_id)
        if account:
            cand = dict(settings_mod.get(db, "candidate", user_id) or {})
            changed = False
            if not cand.get("full_name") and account.get("full_name"):
                cand["full_name"], changed = account["full_name"], True
            if not cand.get("email") and account.get("email"):
                cand["email"], changed = account["email"], True
            if changed:
                settings_mod.set_value(db, "candidate", cand, actor, user_id=user_id)
    except SettingsError as e:
        raise OnboardingError(str(e)) from None
    now = iso()
    c = _save_career(db, user_id, actor, onboarded_at=now)
    with db.transaction():
        db.run("UPDATE users SET onboarded_at=? WHERE id=? AND onboarded_at IS NULL", (now, user_id))
        audit(db, actor=actor, entity="onboarding", entity_id=user_id, action="onboarding_finished",
              detail={"industries": c["industries"], "roles": c["roles"], "custom": len(c["custom_roles"]),
                      "experience": c["experience"]})
    return c


def mark_location_saved(db: Database, user_id: int, actor: str) -> None:
    audit(db, actor=actor, entity="onboarding", entity_id=user_id, action="location_saved")


def summary(db: Database, user_id: int) -> dict:
    """What the user chose, for headers and the review step."""
    s = settings_mod.get_all(db, user_id)
    c = s.get("career") or {}
    return {
        "industries": [taxonomy.industry_label(i) for i in c.get("industries") or []],
        "roles": [taxonomy.role_label(r) for r in c.get("roles") or []] + list(c.get("custom_roles") or []),
        "experience": taxonomy.EXPERIENCE.get(c.get("experience") or "", ("", ""))[0],
        "types": [taxonomy.EMPLOYMENT_TYPES[t] for t in c.get("employment_types") or [] if t in
                  taxonomy.EMPLOYMENT_TYPES],
        "countries": [COUNTRY_NAMES.get(x, x) for x in settings_mod.all_target_countries(s)],
        "arrangements": s.get("work_arrangements") or [],
        "has_career": taxonomy.has_career(c),
        "needs_sponsorship": bool((s.get("candidate") or {}).get("requires_sponsorship_outside")),
    }
