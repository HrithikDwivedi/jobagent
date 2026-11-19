"""Which screens each user may open, decided by the administrator.

Three layers, most specific wins:
  1. per user ("Custom"): users.screens = JSON list of screens turned OFF for that person,
  2. per industry ("Industry"): settings key `segment_screens` = {industry: [screens off]}, applied to everyone who
     targets that industry (a screen is hidden only if it is off for ALL of the user's industries),
  3. automatic defaults: Top MNCs only for technology/finance/business seekers, Sponsorship only for people who
     need a visa.
Hidden screens disappear from the menu AND are refused by the server (the extension API too, for the Companion).
The administrator always sees everything. Core screens (Overview, Jobs, Applications, Job preferences, Settings,
Help & Support) can never be turned off."""
from __future__ import annotations

from . import settings as settings_mod
from . import taxonomy
from .db import Database, audit, dumps, loads

SCREENS: tuple[tuple[str, str], ...] = (
    ("/mnc", "Top MNCs"),
    ("/answers", "Screening Answers"),
    ("/recruiters", "Recruiters"),
    ("/companion", "Application Companion"),
    ("/sponsorship", "Sponsorship & Relocation"),
    ("/prep", "Skills & Prep"),
    ("/studio", "Resume Studio"),
    ("/runs", "Agent Runs"),
    ("/audit", "Audit Log"),
)
SCREEN_PATHS = tuple(p for p, _ in SCREENS)
LABELS = dict(SCREENS)
CORE = ("/", "/jobs", "/applications", "/welcome", "/settings", "/support")
MNC_INDUSTRIES = {"technology", "finance", "business"}


class AccessError(Exception):
    pass


def default_industry_hidden(industry: str) -> list[str]:
    return [] if industry in MNC_INDUSTRIES else ["/mnc"]


def segment_rules(db: Database) -> dict[str, list[str]]:
    """{industry: [screens off]} for every industry (administrator's choice, else the default)."""
    saved = settings_mod.get(db, "segment_screens", None) or {}
    return {i.id: list(saved.get(i.id, default_industry_hidden(i.id))) for i in taxonomy.INDUSTRIES}


def clean(paths) -> list[str]:
    return [p for p in dict.fromkeys(paths or []) if p in SCREEN_PATHS]


def resolve(db: Database, user_id: int, *, settings: dict | None = None) -> dict:
    """{'hidden': [...], 'source': 'admin'|'custom'|'industry'|'automatic'}"""
    u = db.one("SELECT role, screens FROM users WHERE id=?", (user_id,))
    if not u or u["role"] == "admin":
        return {"hidden": [], "source": "admin"}
    if u["screens"] is not None:
        return {"hidden": clean(loads(u["screens"], [])), "source": "custom"}
    s = settings or settings_mod.get_all(db, user_id)
    inds = list((s.get("career") or {}).get("industries") or [])
    hidden: list[str] = []
    source = "automatic"
    if inds:
        rules = segment_rules(db)
        sets = [set(rules.get(i, [])) for i in inds]
        hidden = sorted(set.intersection(*sets)) if sets else []
        saved = settings_mod.get(db, "segment_screens", None) or {}
        if any(i in saved for i in inds):
            source = "industry"
    if not (s.get("candidate") or {}).get("requires_sponsorship_outside", True) and "/sponsorship" not in hidden:
        hidden.append("/sponsorship")
    return {"hidden": [p for p in SCREEN_PATHS if p in hidden], "source": source}


def blocked(hidden: list[str], path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in hidden)


def set_user(db: Database, user_id: int, mode: str, visible: list[str], actor: str) -> None:
    """mode 'auto' clears the override; 'custom' stores the screens that are OFF (all minus `visible`)."""
    u = db.one("SELECT username, role FROM users WHERE id=?", (user_id,))
    if not u:
        raise AccessError("User not found.")
    if u["role"] == "admin":
        raise AccessError("The administrator always sees every screen.")
    if mode not in ("auto", "custom"):
        raise AccessError("Choose automatic or custom screens.")
    off = None if mode == "auto" else [p for p in SCREEN_PATHS if p not in set(clean(visible))]
    with db.transaction():
        db.run("UPDATE users SET screens=? WHERE id=?", (None if off is None else dumps(off), user_id))
        audit(db, actor=actor, entity="admin", entity_id=user_id, action="screens_set",
              detail={"username": u["username"], "mode": mode, "off": off})


def set_segment(db: Database, industry: str, visible: list[str], actor: str) -> None:
    if industry not in taxonomy.BY_ID:
        raise AccessError("Unknown industry.")
    saved = dict(settings_mod.get(db, "segment_screens", None) or {})
    saved[industry] = [p for p in SCREEN_PATHS if p not in set(clean(visible))]
    settings_mod.set_value(db, "segment_screens", saved, actor)


def matrix(db: Database) -> list[dict]:
    """Who sees what: one row per approved account."""
    rows = []
    for u in db.all("SELECT id, username, full_name, role FROM users WHERE status='approved' ORDER BY id"):
        r = resolve(db, u["id"])
        rows.append({"id": u["id"], "name": u["full_name"] or u["username"], "username": u["username"],
                     "role": u["role"], "source": r["source"], "hidden": r["hidden"],
                     "visible": [p for p in SCREEN_PATHS if p not in r["hidden"]]})
    return rows
