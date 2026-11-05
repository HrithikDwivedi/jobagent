"""User-confirmed edits to the canonical (master) resume content.

These change FACTS, so they are only applied when the user explicitly asks for them; the result is stored as a
new canonical version via store.create_edited_canonical(...). The original resume file is never touched."""
from __future__ import annotations

import copy
import re

from ..skills import find_skills
from .variants import render_text

# User-provided E.M.A. facts (confirmed 2026-11-04): E.M.A. IS the Empower support chatbot, now on Google Gemini.
# Wording lightly tightened (past-tense openers, Gemini kept out of parentheses); no facts or numbers added.
EMA_BULLETS = [
    "Built E.M.A., a Google Gemini AI chatbot in the company's enterprise web app that turns plain-English questions "
    "about invoices, forecasts, collections, P&L and orders into live MySQL queries, answering with tables and "
    "figures that match the app's own screens",
    "Hardened E.M.A. for the live database: every generated query is validated as read-only, sensitive data is "
    "filtered out, and role-based scoping limits each user to their own data (Management sees all, a Business Head "
    "sees their BU, everyone else sees themselves and their direct reports)",
    "Implemented table selection by scoring how well each table matches the question, clarifying questions for "
    "ambiguous requests, context for follow-ups, answers from uploaded files and pointers to the right app screen",
    "Added a thumbs-up/down feedback loop with a review queue so answers improve over time, plus chat history, "
    "automatic clean-up of old chats, plain-language error messages, email escalation and an animated chat window",
]
_OLD_CHATBOT = re.compile(r"GPT-powered support chatbot|retrieval-augmented generation \(RAG\)", re.I)
_OLD_ESCALATION = re.compile(r"automated escalation workflow", re.I)
_SUMMARY_OLD = re.compile(r"including a retrieval-augmented \(RAG\) GPT chatbot for real-time user support", re.I)
_SUMMARY_NEW = ("including E.M.A., a Google Gemini assistant that answers business questions from live MySQL data "
                "with role-based access")


def refresh_derived(structured: dict) -> dict:
    """Recompute the fields derived from content (text used by the fact guard, detected skills)."""
    structured["text"] = render_text(structured)
    structured["detected_skills"] = sorted(find_skills(structured["text"]).keys())
    return structured


def apply_ema_update(structured: dict) -> dict:
    """Return a NEW structure with the E.M.A. update applied (input untouched). Idempotent.
    Raises ValueError when the Empower project is not present (e.g. a different resume)."""
    s = copy.deepcopy(structured)
    proj = next((p for r in s.get("experience", []) for p in r.get("projects", [])
                 if (p.get("name") or "").lower().startswith("empower")), None)
    if proj is None:
        raise ValueError("Empower project not found in this resume; E.M.A. update not applicable.")
    bullets = proj.get("bullets", [])
    if not any(b.startswith("Built E.M.A.") for b in bullets):
        idx = [i for i, b in enumerate(bullets) if _OLD_CHATBOT.search(b) or _OLD_ESCALATION.search(b)]
        insert_at = idx[0] if idx else 0
        kept = [b for i, b in enumerate(bullets) if i not in idx]
        proj["bullets"] = kept[:insert_at] + list(EMA_BULLETS) + kept[insert_at:]
    proj["tech"] = ["Google Gemini API" if re.fullmatch(r"OpenAI GPT API", t.strip(), re.I) else t
                    for t in proj.get("tech", [])]
    if s.get("summary"):
        s["summary"] = _SUMMARY_OLD.sub(_SUMMARY_NEW, s["summary"])
    for cat, items in (s.get("skills") or {}).items():
        s["skills"][cat] = [re.sub(r"^(LLM-Assisted Development \()(?!Google Gemini)", r"\1Google Gemini, ", it)
                            for it in items]
    return refresh_derived(s)
