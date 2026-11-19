"""Administrator view of WHO uses the installation: every account is classified by the industries, roles and level
it is looking for (self-reported in onboarding, or inferred from preferred role titles / the resume for accounts
that never answered), grouped into segments, and compared with the jobs actually available for each segment so the
administrator can see where job supply is missing and what to do about it.

Only preferences and counts are shown, never resumes, applications or answers."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from . import settings as settings_mod
from . import taxonomy
from .db import Database, iso, utcnow
from .resume import store as resume_store
from .settings import COUNTRY_NAMES

LOW_SUPPLY_PER_USER = 15        # fewer open jobs than this per interested user = not enough to choose from


def _resume_industry(db: Database, uid: int) -> str | None:
    r = resume_store.canonical(db, user_id=uid)
    if not r:
        return None
    sug = taxonomy.suggest_from_resume(r.get("structured"))
    return (sug.get("industries") or [None])[0]


def profiles(db: Database) -> dict[int, dict]:
    """user id -> {persona, industries, roles, level, countries, needs_sponsorship, has_resume, onboarded}"""
    out = {}
    for u in db.all("SELECT id, role, status, onboarded_at FROM users ORDER BY id"):
        s = settings_mod.get_all(db, u["id"])
        c = s.get("career") or {}
        cand = s.get("candidate") or {}
        res_ind = _resume_industry(db, u["id"])
        needs = bool(cand.get("requires_sponsorship_outside", True))
        p = taxonomy.persona(c, resume_industry=res_ind, needs_sponsorship=needs,
                             remote_only=s.get("work_arrangements") == ["remote"],
                             inferred_from=None if taxonomy.has_career(c) else (s.get("preferred_roles") or [])[:6])
        inds = list(c.get("industries") or []) or ([p["industry"]] if p["industry"] else [])
        roles = list(c.get("roles") or [])
        if not roles and not taxonomy.has_career(c):
            for t in (s.get("preferred_roles") or [])[:6]:
                _, rid = taxonomy.classify(t)
                if rid and rid not in roles:
                    roles.append(rid)
        out[u["id"]] = {
            "persona": p, "industries": inds, "roles": roles[:3], "custom": list(c.get("custom_roles") or []),
            "level": c.get("experience"), "countries": settings_mod.all_target_countries(s),
            "needs_sponsorship": needs, "has_resume": res_ind is not None or
            resume_store.canonical(db, user_id=u["id"]) is not None,
            "self_reported": taxonomy.has_career(c), "status": u["status"], "onboarded": bool(u["onboarded_at"]),
        }
    return out


def overview(db: Database, *, adzuna_configured: bool) -> dict:
    profs = profiles(db)
    active = {uid: p for uid, p in profs.items() if p["status"] == "approved"}
    since = iso(utcnow() - timedelta(days=7))
    supply = {r["industry"]: (int(r["n"]), int(r["new"] or 0)) for r in db.all(
        "SELECT industry, COUNT(*) AS n, SUM(CASE WHEN first_discovered_at >= ? THEN 1 ELSE 0 END) AS new "
        "FROM jobs WHERE status='open' AND is_sample=0 AND industry IS NOT NULL GROUP BY industry", (since,))}
    role_supply = {r["role_key"]: int(r["n"]) for r in db.all(
        "SELECT role_key, COUNT(*) AS n FROM jobs WHERE status='open' AND is_sample=0 AND role_key IS NOT NULL "
        "GROUP BY role_key")}
    segments = []
    for ind in taxonomy.INDUSTRIES:
        members = [p for p in active.values() if ind.id in p["industries"]]
        if not members:
            continue
        roles = Counter(r for p in members for r in p["roles"] if taxonomy.ROLES.get(r, (None,))[0] == ind.id)
        levels = Counter(p["level"] for p in members if p["level"])
        countries = Counter(c for p in members for c in p["countries"])
        n_jobs, n_new = supply.get(ind.id, (0, 0))
        per_user = n_jobs / len(members)
        status = "low" if per_user < LOW_SUPPLY_PER_USER else ("ok" if per_user < 60 else "good")
        actions = []
        if status == "low":
            if not adzuna_configured:
                actions.append("Add free Adzuna API keys (ADZUNA_APP_ID / ADZUNA_APP_KEY) to search "
                               + ", ".join(COUNTRY_NAMES.get(c, c) for c, _ in countries.most_common(3))
                               + " for these roles.")
            if not ind.jobicy:
                actions.append("Add employers in this industry under Settings → Sources (Greenhouse / Lever / "
                               "Ashby boards).")
            else:
                actions.append("Remote jobs for this industry are searched on Jobicy every run.")
        role_rows = [{"id": r, "label": taxonomy.role_label(r), "users": n, "jobs": role_supply.get(r, 0)}
                     for r, n in roles.most_common()]
        segments.append({
            "id": ind.id, "label": ind.label, "icon": ind.icon, "users": len(members), "roles": role_rows,
            "levels": [(taxonomy.EXPERIENCE[k][0].split(" (")[0], v) for k, v in levels.most_common()],
            "countries": [(COUNTRY_NAMES.get(c, c), n) for c, n in countries.most_common(4)],
            "jobs": n_jobs, "new_jobs": n_new, "per_user": round(per_user, 1), "supply": status, "actions": actions,
            "sponsorship": sum(1 for p in members if p["needs_sponsorship"]),
            "switchers": sum(1 for p in members if "Career switcher" in p["persona"]["tags"]),
        })
    segments.sort(key=lambda s: (-s["users"], s["label"]))
    approved = list(active.values())
    funnel = [
        ("Accounts", len(profs)),
        ("Approved", len(approved)),
        ("Preferences set", sum(1 for p in approved if p["self_reported"])),
        ("Resume uploaded", sum(1 for p in approved if p["has_resume"])),
        ("Applied at least once", int(db.scalar(
            "SELECT COUNT(DISTINCT a.user_id) FROM applications a JOIN users u ON u.id=a.user_id "
            "WHERE u.status='approved' AND a.is_sample=0 AND a.status IN ('submitted','interview','assessment',"
            "'offer','rejected')") or 0)),
    ]
    levels = Counter(p["level"] for p in approved if p["level"])
    unset = [uid for uid, p in active.items() if not p["self_reported"]]
    return {"segments": segments, "funnel": funnel, "profiles": profs, "unset": len(unset),
            "levels": [(taxonomy.EXPERIENCE[k][0], v) for k, v in levels.most_common()],
            "industries_by_users": [(s["label"], s["users"]) for s in segments],
            "low": [s for s in segments if s["supply"] == "low"]}
