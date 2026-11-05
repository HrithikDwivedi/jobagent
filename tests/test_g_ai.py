"""G. AI & cost controls (fake providers and httpx.MockTransport; no real API calls)."""
from __future__ import annotations

import copy

import httpx
import pytest

from factories import ADMIN

from jobagent import agent
from jobagent import settings as settings_mod
from jobagent.ai import (AIError, AIService, BudgetExceeded, Completion, OpenAIProvider, Provider, letter_violations,
                         validate_job_analysis)
from jobagent.db import iso
from wf import add_job

AI = dict(settings_mod.DEFAULT_SETTINGS["ai"], enabled=True, model="gpt-4.1-mini", fallback_model="gpt-4o-mini")
PROFILE = {"skills": {"C#": "demonstrated", ".NET": "demonstrated", "SQL": "demonstrated"}, "experience_years": 2.0,
           "languages": {"english": "C1"}, "work_authorization": {"IN": "citizen"}}


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, model, system, user, max_tokens):
        self.calls.append(model)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return Completion(r, 120, 60, model)


def openai(handler):
    sleeps = []
    p = OpenAIProvider("sk-test-key", transport=httpx.MockTransport(handler), sleep=lambda s: sleeps.append(s))
    p.sleeps = sleeps
    return p


def ok_body(content='{"must_have_skills": []}'):
    return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}}


def test_provider_unavailable_falls_back_to_rules(db):
    """Expected: network errors -> AIError after 3 attempts; scoring falls back to rules and no application is created."""
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        raise httpx.ConnectError("down", request=req)
    p = openai(h)
    with pytest.raises(AIError) as ei:
        p.complete_json("gpt-4.1-mini", "s", "u", 10)
    assert not ei.value.permanent and n["i"] == 3 and len(p.sleeps) == 2
    jid = add_job(db)
    s = settings_mod.get_all(db, ADMIN)
    ai = AIService(db, p, AI, user_id=ADMIN)
    det = agent.score_and_store(db, jid, PROFILE, s, "v1", ai, use_ai=True, user_id=ADMIN)
    assert "error" in det["ai"]
    assert db.scalar("SELECT method FROM sponsorship_assessments WHERE job_id=?", (jid,)) == "rules"
    assert db.scalar("SELECT COUNT(*) FROM job_scores WHERE job_id=?", (jid,)) == 1
    assert db.scalar("SELECT COUNT(*) FROM applications") == 0


def test_invalid_api_key_disables_ai(db):
    """Expected: HTTP 401 -> permanent AIError and the AIService is disabled for the rest of the run."""
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(401, json={"error": "bad key"})
    ai = AIService(db, openai(h), AI, user_id=ADMIN)
    with pytest.raises(AIError) as ei:
        ai.json_call("p", "s", "u")
    assert ei.value.permanent and not ai.available
    with pytest.raises(AIError):
        ai.json_call("p", "s", "u2")
    assert n["i"] == 1  # no further provider calls after the key was rejected


def test_rate_limit_then_success():
    """Expected: a 429 (Retry-After: 1) is retried after sleeping and the next 200 response is returned."""
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json=ok_body('{"x": 1}'))
    p = openai(h)
    c = p.complete_json("gpt-4.1-mini", "s", "u", 10)
    assert c.text == '{"x": 1}' and p.sleeps == [1.0]


def test_malformed_model_output_uses_fallback(db):
    """Expected: non-JSON output from the primary model is rejected and the fallback model's JSON is used."""
    p = FakeProvider("Sure! Here is your answer: not json", '{"ok": true}')
    ai = AIService(db, p, AI, user_id=ADMIN)
    assert ai.json_call("p", "s", "u") == {"ok": True}
    assert p.calls == ["gpt-4.1-mini", "gpt-4o-mini"]
    assert db.scalar("SELECT COUNT(*) FROM ai_usage WHERE ok=0 AND error='malformed JSON'") == 1


def test_hallucinated_requirements_dropped():
    """Expected: skills/years not literally supported by the posting are dropped by validate_job_analysis."""
    text = "Requirements: C# and SQL. 2 years of experience with .NET."
    raw = {"must_have_skills": ["C#", "Kubernetes", "Rust"], "nice_to_have_skills": ["Terraform"],
           "min_years_experience": 5, "years_quote": "5 years of experience"}
    out = validate_job_analysis(raw, text)
    assert out["must_have"] == ["C#"] and out["nice_to_have"] == []
    assert out["min_years"] is None and len(out["dropped"]) >= 4


def test_hallucinated_sponsorship_dropped():
    """Expected: a sponsorship claim whose quote is not in the posting is dropped; a verbatim quote is kept."""
    text = "We build payments software. Visa sponsorship is available for this role."
    raw = {"sponsorship_statements": [{"stance": "offered", "quote": "We sponsor all visas worldwide"},
                                      {"stance": "offered", "quote": "Visa sponsorship is available for this role."}]}
    out = validate_job_analysis(raw, text)
    assert out["sponsorship"] == [{"stance": "offered", "quote": "Visa sponsorship is available for this role."}]
    assert any("hallucination" in d for d in out["dropped"])


def test_incorrect_resume_facts_in_letter_rejected():
    """Expected: letter_violations flags numbers and skills that are not in the resume."""
    resume = "Built C# APIs for 5000 users. SQL Server."
    letter = "I led 25 engineers and built Kubernetes platforms in C# serving 5000 users."
    v = letter_violations(letter, resume, {"C#", "SQL Server", "SQL"})
    assert any("'25'" in x for x in v) and any("Kubernetes" in x for x in v)
    assert letter_violations("I built C# APIs for 5000 users.", resume, {"C#", "REST APIs"}) == []


def test_excessive_tokens_run_budget(db):
    """Expected: a call whose estimated cost exceeds the run budget raises BudgetExceeded BEFORE calling the provider."""
    p = FakeProvider('{"ok": 1}')
    ai = AIService(db, p, dict(AI, run_budget_usd=0.000001), user_id=ADMIN)
    with pytest.raises(BudgetExceeded):
        ai.json_call("p", "s", "u" * 200000, max_tokens=4000)
    assert p.calls == []


def test_repeated_request_served_from_cache(db):
    """Expected: an identical second request is served from ai_cache (provider called once, usage row cached=1)."""
    p = FakeProvider('{"answer": 42}')
    ai = AIService(db, p, AI, user_id=ADMIN)
    assert ai.json_call("p", "sys", "same") == {"answer": 42}
    assert ai.json_call("p", "sys", "same") == {"answer": 42}
    assert len(p.calls) == 1
    assert db.scalar("SELECT COUNT(*) FROM ai_usage WHERE cached=1") == 1


def test_monthly_budget_reached(db):
    """Expected: when this month's recorded spend exceeds the monthly budget, BudgetExceeded is raised."""
    db.run("INSERT INTO ai_usage(provider, model, purpose, cost_usd, ok, at) VALUES ('fake','m','p',10.0,1,?)", (iso(),))
    p = FakeProvider('{"ok": 1}')
    with pytest.raises(BudgetExceeded, match="Monthly"):
        AIService(db, p, dict(AI, monthly_budget_usd=5.0), user_id=ADMIN).json_call("p", "s", "u")
    assert p.calls == []


def test_both_models_fail(db):
    """Expected: if primary and fallback models both fail, AIError is raised and nothing is written to applications."""
    p = FakeProvider(AIError("model unavailable"), AIError("fallback unavailable"))
    with pytest.raises(AIError):
        AIService(db, p, AI, user_id=ADMIN).json_call("p", "s", "u")
    assert p.calls == ["gpt-4.1-mini", "gpt-4o-mini"]
    assert db.scalar("SELECT COUNT(*) FROM applications") == 0
    assert db.scalar("SELECT COUNT(*) FROM ai_usage WHERE ok=0") == 2


def test_ai_disabled_in_settings_never_calls(db):
    """Expected: with AI disabled in Settings the service is unavailable and the provider is never called."""
    p = FakeProvider('{"ok": 1}')
    ai = AIService(db, p, dict(AI, enabled=False), user_id=ADMIN)
    assert not ai.available
    with pytest.raises(AIError):
        ai.json_call("p", "s", "u")
    assert p.calls == []
