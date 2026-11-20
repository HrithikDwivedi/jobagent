"""The user's application details (Settings → Candidate facts) kept complete for auto-apply and the Companion.

* `from_resume()` reads contact details from a parsed resume (name, email, phone, LinkedIn, GitHub, location).
* `fill_from_resume()` copies them into the user's candidate facts when a resume is uploaded, ONLY for fields the
  user has not filled in themselves (their own entries always win; nothing is guessed or invented).
* `missing()` lists what auto-apply forms commonly require that is still empty; the home screen asks for these in
  a small pop-up (`save_quick()` stores the answers)."""
from __future__ import annotations

import copy
import re
from datetime import timedelta

from . import settings as settings_mod
from .db import Database, iso, utcnow

# what forms ask for most often; the pop-up asks for the empty ones (in this order)
ESSENTIALS: tuple[tuple[str, str, str, str], ...] = (
    # key, label, input type, hint
    ("full_name", "Full name", "text", "As on your passport or ID"),
    ("email", "Email", "email", "Employers reply here"),
    ("phone", "Phone (with country code)", "tel", "e.g. +91 98765 43210"),
    ("current_location", "Where you live", "text", "City, Country, e.g. Mumbai, India"),
    ("notice_period", "Notice period / earliest start", "text", "e.g. Immediately, 30 days, 2 months"),
    ("expected_salary", "Expected salary", "salary", "Per year, in your currency"),
)
LABELS = {k: label for k, label, _t, _h in ESSENTIALS}
FILLED_LABELS = {"full_name": "name", "email": "email", "phone": "phone", "current_location": "location",
                 "linkedin_url": "LinkedIn", "github_url": "GitHub"}
SNOOZE_DAYS = 3


def from_resume(structured: dict | None) -> dict:
    """Candidate-fact keys found in the resume's header (only well-formed values)."""
    r = structured or {}
    contact = r.get("contact") if isinstance(r.get("contact"), dict) else {}
    out: dict = {}
    name = " ".join(str(r.get("name") or "").split())
    if name and 1 < len(name) <= 80 and not re.search(r"[@\d/|]", name):
        out["full_name"] = name.title() if name.isupper() else name
    email = str(contact.get("email") or "").strip()
    if re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
        out["email"] = email
    phone = str(contact.get("phone") or "").strip()
    if re.match(r"^\+?[0-9 ()-]{7,20}$", phone):
        out["phone"] = phone
    loc = str(contact.get("location") or "").strip()
    if loc and len(loc) <= 80 and not re.search(r"[@/]|\d{5,}", loc):
        out["current_location"] = loc
    li = str(contact.get("linkedin") or "").strip().rstrip("/")
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9_-]+)", li, re.I)
    if m:
        out["linkedin_url"] = f"https://www.linkedin.com/in/{m.group(1)}"
    gh = re.search(r"github\.com/([A-Za-z0-9_-]+)", str(contact.get("github") or ""), re.I)
    if gh:
        out["github_url"] = f"https://github.com/{gh.group(1)}"
    return out


def fill_from_resume(db: Database, user_id: int, structured: dict | None, actor: str = "system") -> list[str]:
    """Copy resume contact details into the user's EMPTY candidate fields. Returns the keys filled."""
    found = from_resume(structured)
    if not found:
        return []
    cand = copy.deepcopy(settings_mod.get(db, "candidate", user_id) or {})
    filled = [k for k, v in found.items() if v and not cand.get(k)]
    if not filled:
        return []
    for k in filled:
        cand[k] = found[k]
    try:
        settings_mod.set_value(db, "candidate", cand, actor, user_id=user_id)
    except settings_mod.SettingsError:
        # one odd value must not block the rest: store the fields one by one
        filled_ok = []
        for k in filled:
            trial = copy.deepcopy(settings_mod.get(db, "candidate", user_id) or {})
            trial[k] = found[k]
            try:
                settings_mod.set_value(db, "candidate", trial, actor, user_id=user_id)
                filled_ok.append(k)
            except settings_mod.SettingsError:
                continue
        filled = filled_ok
    return filled


def missing(db: Database, user_id: int, *, settings: dict | None = None) -> list[dict]:
    """Essentials still empty: [{key, label, type, hint}]."""
    s = settings or settings_mod.get_all(db, user_id)
    cand = s.get("candidate") or {}
    out = []
    for key, label, typ, hint in ESSENTIALS:
        if not cand.get(key):
            out.append({"key": key, "label": label, "type": typ, "hint": hint})
    return out


def prompt(db: Database, user_id: int, *, settings: dict | None = None) -> dict | None:
    """What the home pop-up should ask now, or None (nothing missing, or snoozed)."""
    s = settings or settings_mod.get_all(db, user_id)
    until = s.get("profile_prompt_snooze") or ""
    if until and until > iso():
        return None
    gaps = missing(db, user_id, settings=s)
    if not gaps:
        return None
    return {"fields": gaps, "currency": home_currency(s), "currencies": settings_mod.CURRENCIES}


def home_currency(s: dict) -> str:
    """The user's likely salary currency: where they live, else their first target country, else the default."""
    from .fx import COUNTRY_CURRENCY
    loc = str((s.get("candidate") or {}).get("current_location") or "")
    tail = loc.split(",")[-1].strip().lower()
    by_name = {v.lower(): k for k, v in settings_mod.COUNTRY_NAMES.items()}
    code = by_name.get(tail) or (tail.upper() if tail.upper() in settings_mod.COUNTRY_NAMES else None)
    if not code:
        targets = settings_mod.all_target_countries(s) if s.get("target_countries") else []
        code = targets[0] if targets else None
    cur = COUNTRY_CURRENCY.get(code or "")
    return cur if cur in settings_mod.CURRENCIES else (s.get("currency") or "EUR")


def snooze(db: Database, user_id: int, actor: str) -> None:
    settings_mod.set_value(db, "profile_prompt_snooze", iso(utcnow() + timedelta(days=SNOOZE_DAYS)), actor,
                           user_id=user_id)


def save_quick(db: Database, user_id: int, form: dict, actor: str) -> list[str]:
    """Store the pop-up's answers (only non-empty ones). Raises settings.SettingsError with a readable message."""
    cand = copy.deepcopy(settings_mod.get(db, "candidate", user_id) or {})
    saved = []
    for key, _label, typ, _hint in ESSENTIALS:
        if typ == "salary":
            amount = str(form.get("expected_amount") or "").strip()
            if amount:
                from .fx import parse_amount
                if parse_amount(amount) is None:
                    raise settings_mod.SettingsError("Expected salary must be a number, e.g. 45000 or 15,00,000.")
                cand["expected_salary"] = {"amount": amount, "currency": form.get("expected_currency") or "EUR",
                                           "period": form.get("expected_period") or "year"}
                saved.append(key)
            continue
        v = " ".join(str(form.get(key) or "").split())
        if v:
            cand[key] = v
            saved.append(key)
    if saved:
        settings_mod.set_value(db, "candidate", cand, actor, user_id=user_id)
    return saved
