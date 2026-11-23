"""R. Time-boxed runs (Vercel): work left when the time budget runs out is a note, not an error, and the run hands
over to the next one straight away instead of waiting for the next daily schedule. SYNTHETIC data."""
from __future__ import annotations

import httpx

from conftest import extract_csrf, web_factory
from factories import ADMIN, agent_config, make_user
from jobagent import agent
from jobagent import settings as settings_mod
from jobagent.db import dumps, iso

SECRET = "c" * 32


def test_budget_leftovers_are_notes_not_errors(anydb):
    make_user(anydb, "bea")
    make_user(anydb, "cyril")
    res = agent.run_agent(anydb, agent_config(anydb.url if hasattr(anydb, "url") else "sqlite://"),
                          time_budget_s=1, discover=False, prepare=False)
    assert res["status"] == "partial"
    assert res["errors"] == []
    assert res["pending"] and "continues automatically" in res["pending"][0]
    row = anydb.one("SELECT stats_json, errors_json FROM runs WHERE id=?", (res["run_id"],))
    assert "continues automatically" in row["stats_json"] and row["errors_json"] == "[]"


def _vercel_site(tmp_path, monkeypatch, outcome):
    calls = []
    monkeypatch.setattr(agent, "run_agent", lambda db, cfg, **kw: dict(outcome, trigger=kw.get("trigger")))

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "auth": (headers or {}).get("Authorization")})
        raise httpx.ReadTimeout("still running")              # the next run keeps going on its own
    monkeypatch.setattr(httpx, "get", fake_get)
    w = web_factory(tmp_path, cron_secret=SECRET, deployment="vercel", public_url="https://jobs.example.com")
    return w, calls


def test_scheduled_run_hands_over_when_time_runs_out(tmp_path, monkeypatch):
    w, calls = _vercel_site(tmp_path, monkeypatch, {"run_id": "r1", "status": "partial",
                                                     "pending": ["Job search paused at source 93 of 123"]})
    with w.client:
        r = w.client.get("/api/cron/run", headers={"Authorization": f"Bearer {SECRET}"})
        assert r.json() == {"run_id": "r1", "status": "partial", "continued": True}
        assert calls == [{"url": "https://jobs.example.com/api/cron/run", "params": {"chain": "1"},
                          "auth": f"Bearer {SECRET}"}]
        # a continuation continues the chain ...
        w.client.get("/api/cron/run", params={"chain": "3"}, headers={"Authorization": f"Bearer {SECRET}"})
        assert calls[-1]["params"] == {"chain": "4"}
        # ... but never forever
        r = w.client.get("/api/cron/run", params={"chain": "8"}, headers={"Authorization": f"Bearer {SECRET}"})
        assert r.json()["continued"] is False and len(calls) == 2
        # no secret, no run
        assert w.client.get("/api/cron/run", params={"chain": "1"}).status_code == 403


def test_finished_or_failed_runs_do_not_continue(tmp_path, monkeypatch):
    for outcome in ({"run_id": "r", "status": "succeeded", "pending": []},
                    {"run_id": "r", "status": "failed", "pending": []},
                    {"run_id": "r", "status": "partial", "pending": []}):           # partial for another reason
        w, calls = _vercel_site(tmp_path / outcome["status"] / str(len(outcome["pending"])), monkeypatch, outcome)
        with w.client:
            r = w.client.get("/api/cron/run", headers={"Authorization": f"Bearer {SECRET}"})
            assert r.json()["continued"] is False and calls == []


def test_admin_run_now_continues_in_the_background(tmp_path, monkeypatch):
    w, calls = _vercel_site(tmp_path, monkeypatch, {"run_id": "r9", "status": "partial",
                                                     "pending": ["3 user(s) still to score"]})
    from conftest import TEST_PASSWORD, TEST_USER
    from jobagent.web import security
    w.client.get("/healthz")                      # create the tables
    d = w.db()                                    # on Vercel /setup is locked without a token: create directly
    security.create_user(d, TEST_USER, TEST_PASSWORD, actor="test", role="admin", status="approved")
    d.close()
    assert w.login().status_code == 303
    tok = extract_csrf(w.client.get("/runs").text)
    r = w.client.post("/runs/start", data={"csrf": tok}, follow_redirects=False)
    assert r.headers["location"] == "/runs/r9" and calls and calls[0]["params"] == {"chain": "1"}


def test_old_runs_show_budget_notes_separately(tmp_path):
    w = web_factory(tmp_path)
    w.setup_user()
    d = w.db()
    with d.transaction():
        d.run("INSERT INTO runs(id, kind, trigger, status, started_at, finished_at, config_json, stats_json, "
              "errors_json) VALUES ('old', 'agent', 'scheduled', 'partial', ?, ?, '{}', '{}', ?)",
              (iso(), iso(), dumps(["Time budget reached; will resume at source 93/123 next run.",
                                    "Time budget reached; 5 user(s) will be scored next run.",
                                    "adzuna: HTTP 500"])))
    d.close()
    assert w.login().status_code == 303
    page = w.client.get("/runs/old").text
    assert "Continues automatically" in page and "will resume at source 93/123" in page
    errors = page.split('id="err-h"')[1]
    assert "adzuna: HTTP 500" in errors and "Time budget" not in errors
    listing = w.client.get("/runs").text
    assert 'b-red">1<' in listing.replace(" ", "")  or ">1</span>" in listing
