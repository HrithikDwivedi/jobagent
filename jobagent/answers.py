"""Screening-answer library: answer a recurring application-form question once, and auto-apply reuses it.

The auto-apply worker records every form it could not complete (audit action 'autoapply_handback' with the blocking
questions). This module turns those into a to-do list of questions, stores the user's answers in the
`answer_library` setting, and re-queues the handed-back applications so the next run tries them again."""
from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from . import settings as settings_mod
from .db import Database, audit, iso, loads, parse_iso, utcnow

REASONS = {
    "unknown_questions": "Required questions without a saved answer",
    "captcha": "Visible CAPTCHA / human check on the form",
    "unsupported_ats": "Application site not supported for auto-apply",
    "fill_only_headless": "Fill-only mode needs a visible browser",
    "form_unreachable": "Application page could not be opened",
    "form_not_found": "No application form found on the page",
    "destination_mismatch": "Form sent the browser to a different site",
    "resume_unavailable": "Resume PDF could not be generated",
    "preflight_error": "Unexpected error while reading the form",
}
CONSENT_HINT = re.compile(r"privacy|consent|gdpr|data (processing|protection)|i agree|acknowledg", re.I)


def norm(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (q or "").lower()).strip()


def _handbacks(db: Database, user_id: int) -> list[dict]:
    return db.all(
        "SELECT e.entity_id, e.at, e.detail_json, a.status, a.updated_at FROM audit_events e "
        "JOIN applications a ON e.entity='application' AND e.entity_id = CAST(a.id AS TEXT) "
        "WHERE a.user_id=? AND e.action='autoapply_handback' ORDER BY e.id DESC LIMIT 2000", (user_id,))


def library(db: Database, user_id: int) -> list[dict]:
    return list(settings_mod.get(db, "answer_library", user_id) or [])


def overview(db: Database, *, user_id: int) -> dict:
    """{pending: [{question, apps, consent}], reasons: [(code, label, n)], library: [...]}
    Only the LATEST hand-back of each application that is still waiting counts."""
    lib = {norm(x["question"]) for x in library(db, user_id)}
    latest: dict[str, dict] = {}
    for r in _handbacks(db, user_id):
        latest.setdefault(r["entity_id"], r)
    reasons: Counter = Counter()
    questions: dict[str, dict] = {}
    for app_id, r in latest.items():
        if r["status"] not in ("awaiting_approval",):
            continue
        d = loads(r["detail_json"], {}) or {}
        reasons[d.get("code") or "unknown"] += 1
        for q in d.get("unknown_questions") or []:
            q = " ".join(str(q).split())
            if not q or q.startswith(("Resume upload field", "No email field")):
                continue
            k = norm(q)
            if k in lib:
                continue
            item = questions.setdefault(k, {"question": q, "apps": set(), "consent": bool(CONSENT_HINT.search(q))})
            item["apps"].add(int(app_id))
    pending = sorted(({"question": v["question"], "apps": sorted(v["apps"]), "consent": v["consent"]}
                      for v in questions.values()), key=lambda x: (-len(x["apps"]), x["question"].lower()))
    return {"pending": pending,
            "reasons": [(k, REASONS.get(k, k.replace("_", " ").capitalize()), n) for k, n in reasons.most_common()],
            "library": library(db, user_id)}


def save(db: Database, *, user_id: int, actor: str, items: list[dict], requeue: bool = True) -> dict:
    """Add/replace answers (by normalised question) and re-queue handed-back applications."""
    lib = library(db, user_id)
    by_key = {norm(x["question"]): i for i, x in enumerate(lib)}
    changed = 0
    for it in items:
        q, a = " ".join(str(it.get("question", "")).split()), str(it.get("answer", "")).strip()
        if not q or not a:
            continue
        k = norm(q)
        if k in by_key:
            if lib[by_key[k]]["answer"] != a:
                lib[by_key[k]] = {"question": q, "answer": a}
                changed += 1
        else:
            by_key[k] = len(lib)
            lib.append({"question": q, "answer": a})
            changed += 1
    if changed:
        settings_mod.set_value(db, "answer_library", lib, actor, user_id=user_id)
    n = requeue_handed_back(db, user_id=user_id, actor=actor) if changed and requeue else 0
    return {"saved": changed, "requeued": n}


def delete(db: Database, *, user_id: int, actor: str, question: str) -> bool:
    lib = library(db, user_id)
    k = norm(question)
    new = [x for x in lib if norm(x["question"]) != k]
    if len(new) == len(lib):
        return False
    settings_mod.set_value(db, "answer_library", new, actor, user_id=user_id)
    return True


def requeue_handed_back(db: Database, *, user_id: int, actor: str) -> int:
    """The worker skips an application handed back since its last change; touching it makes the next run retry."""
    last: dict[int, str] = {}
    for r in _handbacks(db, user_id):
        if r["status"] == "awaiting_approval":
            i = int(r["entity_id"])
            last[i] = max(last.get(i, ""), r["at"] or "")
    ids = sorted(last)
    if not ids:
        return 0
    now = utcnow()
    with db.transaction():
        for i in ids:
            # timestamps have 1 s resolution: stamp strictly after the hand-back so the worker picks it up again
            handed = parse_iso(last[i])
            stamp = max(now, handed + timedelta(seconds=1)) if handed else now
            db.run("UPDATE applications SET updated_at=? WHERE id=? AND user_id=?", (iso(stamp), i, user_id))
    audit(db, actor=actor, entity="application", entity_id=None, action="autoapply_requeued",
          detail={"applications": ids[:200], "why": "screening answers updated"})
    return len(ids)
