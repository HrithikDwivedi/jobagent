"""User-editable settings stored in the DB, with defaults and strict server-side validation."""
from __future__ import annotations

import copy
import re
from typing import Any

from .db import Database, audit, dumps, iso, loads

COUNTRY_NAMES = {
    "IE": "Ireland", "GB": "United Kingdom", "DE": "Germany", "NL": "Netherlands", "SE": "Sweden",
    "DK": "Denmark", "FI": "Finland", "AT": "Austria", "CH": "Switzerland", "BE": "Belgium",
    "LU": "Luxembourg", "IT": "Italy", "PT": "Portugal", "ES": "Spain", "PL": "Poland", "CZ": "Czechia",
    "EE": "Estonia", "FR": "France", "NO": "Norway", "LT": "Lithuania", "LV": "Latvia", "RO": "Romania",
    "GR": "Greece", "HU": "Hungary", "MT": "Malta", "CY": "Cyprus", "IN": "India", "US": "United States",
    "CA": "Canada", "AU": "Australia", "AE": "United Arab Emirates", "SG": "Singapore", "JP": "Japan",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    # Candidate facts confirmed by the user (never inferred by AI). Personal values are NOT kept in code:
    # they come from Settings (stored in the database) or the private JOBAGENT_CANDIDATE_JSON env variable.
    "candidate": {
        "full_name": None,
        "email": None,
        "phone": None,
        "linkedin_url": None,
        "github_url": None,
        "portfolio_url": None,
        "current_location": None,
        "nationality": None,
        "citizenship_countries": [],
        "work_authorization": {},                     # countries where no sponsorship is needed
        "requires_sponsorship_outside": True,
        "languages": [{"language": "English", "level": "unverified"}],
        "notice_period": None,                        # must be set by the user before it is used in answers
        "salary_expectation": None,
        "willing_to_relocate": True,
    },
    "target_countries": {
        "tier1": ["IE", "GB", "CA", "AU", "AE"],
        "tier2": ["DE", "NL", "SE", "DK", "FI", "AT", "CH", "BE", "LU"],
        "tier3": ["IT", "PT", "ES", "PL", "CZ", "EE", "FR", "NO", "LT", "JP"],
    },
    "include_india_remote": True,
    "preferred_roles": [
        "software engineer", "backend engineer", ".net developer", "c# developer", "full stack engineer",
        "python developer", "application engineer", "integration engineer", "ai engineer",
    ],
    "excluded_title_terms": ["senior", "sr.", "staff", "principal", "lead", "head of", "director",
                             "manager", "architect", "vp ", "intern", "working student", "werkstudent"],
    "min_salary": 0,
    "currency": "EUR",
    "sponsorship_policy": "exclude_not_supported",   # exclude_not_supported | require_confirmed_or_likely | any
    "work_arrangements": ["onsite", "hybrid", "remote"],
    "max_applications_per_run": 5,
    "max_applications_per_day": 10,
    "max_applications_per_week": 30,
    "min_match_score": 55,
    "weights": {
        "technical": 30, "experience": 20, "sponsorship": 20, "role": 15, "location_language": 10,
        "compensation": 5,
    },
    "sources": {
        "arbeitnow": {"enabled": True, "max_pages": 3},
        "greenhouse": {"enabled": True, "boards": [
            "intercom", "stripe", "datadog", "cloudflare", "gitlab", "mongodb", "elastic", "n26",
            "celonis", "adyen", "wise", "monzo", "deliveroo", "getyourguide", "contentful", "sumup",
            "trivago", "dropbox", "squarespace", "toast", "airbnb",
            # Canada / Australia / UAE / Japan and global employers with offices there
            "careem", "cultureamp", "paypay", "faire", "geotab", "dialpad", "coinbase", "okta", "twilio",
            "figma", "asana", "databricks"]},
        "lever": {"enabled": True, "sites": ["spotify", "palantir", "pigment", "swile", "contentsquare", "deputy",
                                             "immutable"]},
        "ashby": {"enabled": True, "boards": ["n8n", "linear", "synthesia", "elevenlabs", "ramp", "wealthsimple",
                                              "airwallex", "hopper", "koho", "1password", "cohere", "xero",
                                              "benevity", "jobber", "lightspeedhq", "miro", "notion", "snowflake"]},
        "uk_sponsor_register": {"enabled": True, "max_age_days": 7},
    },
    "ai": {
        "enabled": True,
        "model": "gpt-4.1-mini",
        "fallback_model": "gpt-4o-mini",
        "run_budget_usd": 0.50,
        "monthly_budget_usd": 5.00,
        "max_jobs_analyzed_per_run": 15,
    },
    # Auto-apply: OFF until the user enables it AND acknowledges the policy. Applies only to jobs that pass
    # every gate below; anything unknown is handed back to the user.
    "mnc": {
        "enabled": True,
        "companies": [],                     # empty = every catalogue company that has a permitted feed
        "max_pages": 2,
        "fetch_details": True,
        "only_mnc_auto_apply": False,
    },
    "auto_apply": {
        "enabled": False,
        "acknowledged_at": None,
        "mode": "fill_and_submit",          # fill_and_submit | fill_only (user clicks Submit)
        "min_score": 75,
        "sponsorship": ["confirmed", "likely", "not_needed"],
        "countries": [],                    # empty = all target countries
        "ats": ["greenhouse", "lever", "ashby"],
        "max_per_run": 3,
        "max_per_day": 5,
        "headless": True,
        "accept_privacy_notices": False,    # tick REQUIRED privacy/data-processing consent boxes (legal ack)
    },
    "resume_style": {
        "company_type": "auto",             # auto | big_tech | enterprise | startup
        "spelling": "auto",                 # auto (by country) | uk | us
        "one_page": True,
        "ai_rewrite": True,
        "tailor_per_job": True,
        "ats_target": 95,
    },
    "operating_mode": "review_and_approval",     # the only mode: final submission always needs approval
    "approval_ttl_hours": 72,
    "verification_max_age_hours": 48,
    "uncertain_after_hours": 24,
    "notifications": {"desktop": False},
    "data_retention_days": 730,
    "schedule": {"enabled": False, "time": "08:30", "days": ["MON", "TUE", "WED", "THU", "FRI"]},
}

_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class SettingsError(ValueError):
    pass


def _country_list(v: Any, name: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(c, str) for c in v):
        raise SettingsError(f"{name} must be a list of country codes")
    out = []
    for c in v:
        c = c.strip().upper()
        if c not in COUNTRY_NAMES:
            raise SettingsError(f"{name}: unknown country code '{c}'")
        if c not in out:
            out.append(c)
    return out


def _int(v: Any, name: str, lo: int, hi: int) -> int:
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be an integer") from None
    if not lo <= i <= hi:
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    return i


def _float(v: Any, name: str, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a number") from None
    if not lo <= f <= hi:
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    return f


def _str_list(v: Any, name: str, max_items: int = 50, max_len: int = 60) -> list[str]:
    if not isinstance(v, list):
        raise SettingsError(f"{name} must be a list")
    out = []
    for s in v[:max_items]:
        if not isinstance(s, str):
            raise SettingsError(f"{name} entries must be text")
        s = s.strip().lower()
        if s and len(s) <= max_len and s not in out:
            out.append(s)
    return out


def validate(key: str, value: Any) -> Any:
    """Validate and normalise one setting. Raises SettingsError."""
    if key not in DEFAULT_SETTINGS:
        raise SettingsError(f"Unknown setting '{key}'")
    if key == "target_countries":
        if not isinstance(value, dict):
            raise SettingsError("target_countries must be an object with tier1/tier2/tier3")
        return {t: _country_list(value.get(t, []), t) for t in ("tier1", "tier2", "tier3")}
    if key in ("preferred_roles", "excluded_title_terms"):
        return _str_list(value, key)
    if key in ("include_india_remote",):
        return bool(value)
    if key == "min_salary":
        return _int(value, key, 0, 1_000_000)
    if key == "currency":
        if value not in ("EUR", "GBP", "CHF", "SEK", "DKK", "PLN", "CZK", "USD", "INR"):
            raise SettingsError("Unsupported currency")
        return value
    if key == "sponsorship_policy":
        if value not in ("exclude_not_supported", "require_confirmed_or_likely", "any"):
            raise SettingsError("Invalid sponsorship policy")
        return value
    if key == "work_arrangements":
        vals = _str_list(value, key)
        if not vals or any(v not in ("onsite", "hybrid", "remote") for v in vals):
            raise SettingsError("work_arrangements must contain onsite/hybrid/remote")
        return vals
    if key == "max_applications_per_run":
        return _int(value, key, 0, 50)
    if key == "max_applications_per_day":
        return _int(value, key, 0, 100)
    if key == "max_applications_per_week":
        return _int(value, key, 0, 300)
    if key == "min_match_score":
        return _int(value, key, 0, 100)
    if key == "weights":
        if not isinstance(value, dict):
            raise SettingsError("weights must be an object")
        w = {k: _float(value.get(k, 0), f"weights.{k}", 0, 100) for k in DEFAULT_SETTINGS["weights"]}
        if sum(w.values()) <= 0:
            raise SettingsError("At least one weight must be positive")
        return w
    if key == "sources":
        if not isinstance(value, dict):
            raise SettingsError("sources must be an object")
        out = copy.deepcopy(DEFAULT_SETTINGS["sources"])
        for name, conf in value.items():
            if name not in out or not isinstance(conf, dict):
                raise SettingsError(f"Unknown source '{name}'")
            out[name]["enabled"] = bool(conf.get("enabled", out[name]["enabled"]))
            for list_key in ("boards", "sites"):
                if list_key in out[name] and list_key in conf:
                    slugs = _str_list(conf[list_key], f"{name}.{list_key}", 200, 63)
                    bad = [s for s in slugs if not _SLUG.match(s)]
                    if bad:
                        raise SettingsError(f"{name}: invalid board identifiers {bad}")
                    out[name][list_key] = slugs
            if "max_pages" in out[name] and "max_pages" in conf:
                out[name]["max_pages"] = _int(conf["max_pages"], f"{name}.max_pages", 1, 20)
            if "max_age_days" in out[name] and "max_age_days" in conf:
                out[name]["max_age_days"] = _int(conf["max_age_days"], f"{name}.max_age_days", 1, 90)
        return out
    if key == "ai":
        if not isinstance(value, dict):
            raise SettingsError("ai must be an object")
        model_re = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
        model = str(value.get("model", "")).strip()
        fallback = str(value.get("fallback_model", "")).strip()
        if not model_re.match(model) or (fallback and not model_re.match(fallback)):
            raise SettingsError("Invalid model name")
        return {
            "enabled": bool(value.get("enabled", True)),
            "model": model,
            "fallback_model": fallback,
            "run_budget_usd": _float(value.get("run_budget_usd", 0.5), "ai.run_budget_usd", 0, 100),
            "monthly_budget_usd": _float(value.get("monthly_budget_usd", 5), "ai.monthly_budget_usd", 0, 1000),
            "max_jobs_analyzed_per_run": _int(value.get("max_jobs_analyzed_per_run", 15),
                                              "ai.max_jobs_analyzed_per_run", 0, 200),
        }
    if key == "mnc":
        if not isinstance(value, dict):
            raise SettingsError("mnc must be an object")
        from .mnc import catalog
        keys = {c["key"] for c in catalog()}
        comps = [c for c in value.get("companies", []) if isinstance(c, str)]
        bad = [c for c in comps if c not in keys]
        if bad:
            raise SettingsError(f"Unknown MNC companies: {bad}")
        return {"enabled": bool(value.get("enabled", True)), "companies": comps,
                "max_pages": _int(value.get("max_pages", 2), "mnc.max_pages", 1, 10),
                "fetch_details": bool(value.get("fetch_details", True)),
                "only_mnc_auto_apply": bool(value.get("only_mnc_auto_apply", False))}
    if key == "auto_apply":
        if not isinstance(value, dict):
            raise SettingsError("auto_apply must be an object")
        mode = value.get("mode", "fill_and_submit")
        if mode not in ("fill_and_submit", "fill_only"):
            raise SettingsError("auto_apply.mode must be fill_and_submit or fill_only")
        spons = [x for x in value.get("sponsorship", []) if x in ("confirmed", "likely", "unknown", "not_needed")]
        ats = [x for x in value.get("ats", []) if x in ("greenhouse", "lever", "ashby")]
        enabled = bool(value.get("enabled", False))
        ack = value.get("acknowledged_at")
        if enabled and not ack:
            raise SettingsError("Auto-apply can only be enabled after you acknowledge the auto-apply policy.")
        return {
            "enabled": enabled, "acknowledged_at": str(ack)[:40] if ack else None, "mode": mode,
            "min_score": _int(value.get("min_score", 75), "auto_apply.min_score", 50, 100),
            "sponsorship": spons, "countries": _country_list(value.get("countries", []), "auto_apply.countries"),
            "ats": ats,
            "max_per_run": _int(value.get("max_per_run", 3), "auto_apply.max_per_run", 0, 20),
            "max_per_day": _int(value.get("max_per_day", 5), "auto_apply.max_per_day", 0, 50),
            "headless": bool(value.get("headless", True)),
            "accept_privacy_notices": bool(value.get("accept_privacy_notices", False)),
        }
    if key == "resume_style":
        if not isinstance(value, dict):
            raise SettingsError("resume_style must be an object")
        ct = value.get("company_type", "auto")
        sp = value.get("spelling", "auto")
        if ct not in ("auto", "big_tech", "enterprise", "startup"):
            raise SettingsError("Invalid company type")
        if sp not in ("auto", "uk", "us"):
            raise SettingsError("Invalid spelling preference")
        return {"company_type": ct, "spelling": sp, "one_page": bool(value.get("one_page", True)),
                "ai_rewrite": bool(value.get("ai_rewrite", True)),
                "tailor_per_job": bool(value.get("tailor_per_job", True)),
                "ats_target": _int(value.get("ats_target", 95), "resume_style.ats_target", 60, 100)}
    if key == "operating_mode":
        if value != "review_and_approval":
            raise SettingsError("Only 'review_and_approval' mode is supported: final submission always "
                                "requires job-specific approval.")
        return value
    if key == "approval_ttl_hours":
        return _int(value, key, 1, 336)
    if key == "verification_max_age_hours":
        return _int(value, key, 1, 336)
    if key == "uncertain_after_hours":
        return _int(value, key, 1, 336)
    if key == "notifications":
        if not isinstance(value, dict):
            raise SettingsError("notifications must be an object")
        return {"desktop": bool(value.get("desktop", False))}
    if key == "data_retention_days":
        return _int(value, key, 30, 3650)
    if key == "schedule":
        if not isinstance(value, dict):
            raise SettingsError("schedule must be an object")
        t = str(value.get("time", "08:30"))
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
            raise SettingsError("schedule.time must be HH:MM")
        days = [d.upper() for d in value.get("days", []) if isinstance(d, str)]
        if not days or any(d not in _DAYS for d in days):
            raise SettingsError("schedule.days must be a non-empty list of MON..SUN")
        return {"enabled": bool(value.get("enabled", False)), "time": t, "days": days}
    if key == "candidate":
        return _validate_candidate(value)
    raise SettingsError(f"No validator for '{key}'")  # pragma: no cover


def _validate_candidate(value: Any) -> dict:
    if not isinstance(value, dict):
        raise SettingsError("candidate must be an object")
    base = copy.deepcopy(DEFAULT_SETTINGS["candidate"])
    out = {}
    for k in base:
        out[k] = value.get(k, base[k])
    email = str(out["email"] or "")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
        raise SettingsError("Invalid email address")
    phone = str(out["phone"] or "")
    if phone and not re.match(r"^\+?[0-9 ()-]{7,20}$", phone):
        raise SettingsError("Invalid phone number")
    url = str(out["linkedin_url"] or "")
    if url and not re.match(r"^https://(www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?$", url):
        raise SettingsError("LinkedIn URL must look like https://www.linkedin.com/in/<handle>")
    for k in ("github_url", "portfolio_url"):
        v = str(out.get(k) or "").strip()
        if v and not re.match(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/[^\s]*)?$", v):
            raise SettingsError(f"{k.replace('_', ' ')} must be an https:// link")
        out[k] = v or None
    out["citizenship_countries"] = _country_list(out["citizenship_countries"] or [], "citizenship_countries")
    if not isinstance(out["work_authorization"], dict):
        raise SettingsError("work_authorization must be an object")
    wa = {}
    for c, status in out["work_authorization"].items():
        c = c.upper()
        if c not in COUNTRY_NAMES or status not in ("citizen", "permanent_resident", "work_permit"):
            raise SettingsError(f"Invalid work authorization entry {c}: {status}")
        wa[c] = status
    out["work_authorization"] = wa
    langs = []
    for item in out["languages"] or []:
        if not isinstance(item, dict) or not item.get("language"):
            raise SettingsError("languages entries need a language")
        level = str(item.get("level", "unverified"))
        if level not in ("unverified", "native", "C2", "C1", "B2", "B1", "A2", "A1"):
            raise SettingsError(f"Invalid language level '{level}'")
        langs.append({"language": str(item["language"]).strip().title()[:30], "level": level})
    out["languages"] = langs
    for k in ("full_name", "current_location", "nationality", "notice_period", "salary_expectation"):
        if out[k] is not None:
            out[k] = str(out[k]).strip()[:120] or None
    out["requires_sponsorship_outside"] = bool(out["requires_sponsorship_outside"])
    out["willing_to_relocate"] = bool(out["willing_to_relocate"])
    return out


def env_candidate_defaults() -> dict:
    """Private candidate defaults from JOBAGENT_CANDIDATE_JSON (validated; ignored if malformed)."""
    import json
    import os
    raw = os.environ.get("JOBAGENT_CANDIDATE_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return _validate_candidate({**DEFAULT_SETTINGS["candidate"], **data}) if isinstance(data, dict) else {}
    except (ValueError, SettingsError):
        return {}


# Multi-user split. GLOBAL keys are administrator settings shared by the whole installation (stored in `settings`);
# every other key is private to one user (stored in `user_settings`).
GLOBAL_KEYS = frozenset({"sources", "mnc", "ai", "schedule", "operating_mode"})
USER_KEYS = frozenset(k for k in DEFAULT_SETTINGS if k not in GLOBAL_KEYS)


def _is_admin(db: Database, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return db.scalar("SELECT role FROM users WHERE id=?", (user_id,)) == "admin"


def get_all(db: Database, user_id: int | None) -> dict[str, Any]:
    """Merged settings for one user: defaults <- global (admin) settings <- the user's own settings.
    JOBAGENT_CANDIDATE_JSON defaults apply to the administrator only. user_id=None returns defaults + global keys
    (used by installation-wide work such as discovery)."""
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    for r in db.all("SELECT key, value_json FROM settings"):
        v = loads(r["value_json"])
        if r["key"] in GLOBAL_KEYS and v is not None:
            merged[r["key"]] = v
    if user_id is not None:
        if _is_admin(db, user_id):
            env_cand = env_candidate_defaults()
            if env_cand:
                merged["candidate"] = env_cand
        for r in db.all("SELECT key, value_json FROM user_settings WHERE user_id=?", (user_id,)):
            v = loads(r["value_json"])
            if r["key"] in USER_KEYS and v is not None:
                merged[r["key"]] = v
    return merged


def get(db: Database, key: str, user_id: int | None) -> Any:
    return get_all(db, user_id)[key]


def set_value(db: Database, key: str, value: Any, actor: str = "system", *, user_id: int | None = None) -> Any:
    """Validate and store one setting. Per-user keys require `user_id`; global keys ignore it (callers must
    only let the administrator change them)."""
    clean = validate(key, value)
    with db.transaction():
        if key in GLOBAL_KEYS:
            db.run(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, dumps(clean), iso()))
            audit(db, actor=actor, entity="settings", entity_id=key, action="update", detail={"key": key})
        else:
            if user_id is None:
                raise SettingsError(f"'{key}' is a per-user setting; a user is required")
            db.run(
                "INSERT INTO user_settings(user_id, key, value_json, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (user_id, key, dumps(clean), iso()))
            audit(db, actor=actor, entity="settings", entity_id=key, action="update",
                  detail={"key": key, "user_id": user_id})
    return clean


def approved_user_ids(db: Database) -> list[int]:
    return [int(r["id"]) for r in db.all("SELECT id FROM users WHERE status='approved' ORDER BY id")]


def discovery_settings(db: Database) -> dict[str, Any]:
    """Settings for installation-wide discovery: global keys plus the UNION of every approved user's target
    countries (so each user's scope is fetched once) and India-remote if anyone wants it."""
    s = get_all(db, None)
    users = approved_user_ids(db)
    if not users:
        return s
    tiers = {"tier1": [], "tier2": [], "tier3": []}
    india = False
    for uid in users:
        us = get_all(db, uid)
        india = india or bool(us["include_india_remote"])
        for t in tiers:
            for c in us["target_countries"][t]:
                if c not in tiers["tier1"] + tiers["tier2"] + tiers["tier3"]:
                    tiers[t].append(c)
    s["target_countries"] = tiers
    s["include_india_remote"] = india
    return s


def all_target_countries(s: dict) -> list[str]:
    t = s["target_countries"]
    return t["tier1"] + t["tier2"] + t["tier3"]


def country_tier(s: dict, country: str | None) -> int | None:
    if not country:
        return None
    for i, tier in enumerate(("tier1", "tier2", "tier3"), start=1):
        if country in s["target_countries"][tier]:
            return i
    return None
