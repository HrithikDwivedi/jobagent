"""E. Persistence & concurrency, run against SQLite AND PostgreSQL."""
from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from factories import ADMIN, ensure_admin, DOTNET_JD, FakeSource, agent_config, build_pdf, make_raw, no_network_transport
from jobagent import agent
from jobagent import applications as apps
from jobagent import db as dbmod
from jobagent import settings as settings_mod
from jobagent.db import Database, DatabaseError, IntegrityError, audit, iso, open_db, utcnow, verify_audit_chain
from jobagent.http import HttpClient
from jobagent.ingest import upsert_raw
from jobagent.resume import store
from wf import USER, approved, fp, prepared, started, status


def nohttp():
    return HttpClient(transport=no_network_transport(), sleep=lambda s: None)


def setup_agent(db, tmp_path, monkeypatch, sources):
    settings_mod.set_value(db, "sources", {"uk_sponsor_register": {"enabled": False}}, user_id=ADMIN)
    store.import_original(db, build_pdf(tmp_path / "resume.pdf"), user_id=ADMIN)
    monkeypatch.setattr(agent, "build_sources", lambda h, c: [(s, "acme") for s in sources])


def jobs_for_run():
    return [make_raw(n=1, title="Junior .NET Developer", description_text=DOTNET_JD),
            make_raw(n=2, title="Python Developer", description_text="Python and FastAPI. Requirements: Python")]


@pytest.mark.parametrize("kind", ["sqlite", "postgres"])
def test_database_unavailable(tmp_path, kind):
    """Expected: an unreachable database raises DatabaseError with a clear message and no credential leak."""
    if kind == "sqlite":
        blocker = tmp_path / "file.txt"
        blocker.write_text("x")
        url = "sqlite:///" + str(blocker / "sub" / "x.db")
        expect = "Cannot create database directory"
    else:
        url = "postgresql://postgres:SuperSecretPw@127.0.0.1:1/nope"
        expect = "Cannot connect to PostgreSQL"
    with pytest.raises(DatabaseError) as ei:
        open_db(url)
    assert expect in str(ei.value) and "SuperSecretPw" not in str(ei.value)


def test_failed_migration_rolls_back(anyurl, tmp_path, monkeypatch):
    """Expected: a failing migration leaves no partial tables and no schema_migrations row."""
    mig = tmp_path / "migs"
    for d in ("sqlite", "postgres"):
        (mig / d).mkdir(parents=True)
        (mig / d / "0001_broken.sql").write_text(
            "CREATE TABLE t_ok (id INTEGER PRIMARY KEY);\nINSERT INTO no_such_table VALUES (1);\n", encoding="utf-8")
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", mig)
    d = Database(anyurl)
    try:
        with pytest.raises(DatabaseError) as ei:
            d.migrate()
        assert "rolled back" in str(ei.value)
        if d.dialect == "sqlite":
            assert d.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='t_ok'") == 0
        else:
            assert d.scalar("SELECT to_regclass('public.t_ok')") is None
        assert d.scalar("SELECT COUNT(*) FROM schema_migrations") == 0
    finally:
        d.close()


def test_transaction_rollback_on_exception(anydb):
    """Expected: an exception inside db.transaction() rolls back every write in it."""
    with pytest.raises(ValueError):
        with anydb.transaction():
            anydb.run("INSERT INTO employers(name, normalized_name, created_at) VALUES ('X','x',?)", (iso(),))
            with anydb.transaction():  # nested joins the outer transaction
                anydb.run("INSERT INTO employers(name, normalized_name, created_at) VALUES ('Y','y',?)", (iso(),))
            raise ValueError("boom")
    assert anydb.scalar("SELECT COUNT(*) FROM employers") == 0
    assert not anydb.in_transaction


def test_restart_persists_data_and_dedup(anyurl):
    """Expected: after close/reopen, data persists and the same listing is still recognised as duplicate."""
    d = open_db(anyurl)
    ensure_admin(d)
    s = settings_mod.get_all(d, ADMIN)
    assert upsert_raw(d, make_raw(), s)[0] == "new"
    a = prepared(d, 2, title="Python Developer")
    d.close()
    d2 = open_db(anyurl)
    try:
        assert upsert_raw(d2, make_raw(), settings_mod.get_all(d2, ADMIN))[0] == "duplicate"
        assert status(d2, a["id"]) == "awaiting_approval"
        assert d2.scalar("SELECT COUNT(*) FROM jobs") == 2
        assert d2.migrate() == []  # migrations not re-applied
    finally:
        d2.close()


def test_repeated_agent_runs_idempotent(anydb, tmp_path, monkeypatch):
    """Expected: a second identical agent run creates no new jobs or applications but records its own run."""
    setup_agent(anydb, tmp_path, monkeypatch, [FakeSource(jobs=jobs_for_run())])
    cfg = agent_config(anyurl_of(anydb))
    r1 = agent.run_agent(anydb, cfg, http=nohttp())
    jobs1 = anydb.scalar("SELECT COUNT(*) FROM jobs")
    apps1 = anydb.scalar("SELECT COUNT(*) FROM applications")
    r2 = agent.run_agent(anydb, cfg, http=nohttp())
    assert r1["status"] == r2["status"] == "succeeded", (r1, r2)
    assert r1["stats"]["new_jobs"] == 2 and r2["stats"]["new_jobs"] == 0 and r2["stats"]["duplicates"] == 2
    assert apps1 >= 1 and anydb.scalar("SELECT COUNT(*) FROM applications") == apps1
    assert anydb.scalar("SELECT COUNT(*) FROM jobs") == jobs1
    assert anydb.scalar("SELECT COUNT(*) FROM runs") == 2
    # the agent never goes beyond awaiting_approval
    assert anydb.scalar("SELECT COUNT(*) FROM applications WHERE status NOT IN ('shortlisted','awaiting_approval')") == 0


def anyurl_of(d: Database) -> str:
    return d.url


def test_two_simultaneous_agent_runs(anydb, monkeypatch):
    """Expected: with two concurrent runs (separate connections) exactly one runs and the other is 'aborted'."""
    settings_mod.set_value(anydb, "sources", {"uk_sponsor_register": {"enabled": False}}, user_id=ADMIN)
    in_fetch, release = threading.Event(), threading.Event()

    def on_fetch():
        in_fetch.set()
        release.wait(30)
    src = FakeSource(jobs=[make_raw()], on_fetch=on_fetch)
    monkeypatch.setattr(agent, "build_sources", lambda h, c: [(src, "acme")])
    cfg = agent_config(anydb.url)
    results = {}

    def go(name):
        d = Database(anydb.url)
        try:
            results[name] = agent.run_agent(d, cfg, http=nohttp())
        finally:
            d.close()
    ta = threading.Thread(target=go, args=("a",))
    ta.start()
    assert in_fetch.wait(30)
    tb = threading.Thread(target=go, args=("b",))
    tb.start()
    tb.join(60)
    release.set()
    ta.join(60)
    assert results["a"]["status"] == "succeeded" and results["b"]["status"] == "aborted"
    assert src.calls == 1
    assert anydb.scalar("SELECT COUNT(*) FROM runs WHERE status='aborted'") == 1
    assert anydb.scalar("SELECT COUNT(*) FROM locks") == 0  # lock released


def _race(url, fn, n=2):
    barrier = threading.Barrier(n)
    out = []

    def worker():
        d = Database(url)
        try:
            barrier.wait(10)
            out.append(("ok", fn(d)))
        except apps.WorkflowError as e:
            out.append(("refused", e.code))
        except Exception as e:  # noqa: BLE001
            out.append(("error", repr(e)))
        finally:
            d.close()
    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


def test_concurrent_approvals(anydb):
    """Expected: two simultaneous approvals of the same application -> exactly one succeeds, one active approval."""
    a = prepared(anydb)
    f = fp(anydb, a)
    out = _race(anydb.url, lambda d: apps.approve(d, a["id"], USER, f, user_id=ADMIN))
    assert sorted(k for k, _ in out) == ["ok", "refused"], out
    assert anydb.scalar("SELECT COUNT(*) FROM approvals WHERE application_id=?", (a["id"],)) == 1


def test_concurrent_start(anydb):
    """Expected: two simultaneous starts consume the approval exactly once."""
    a = approved(anydb)
    out = _race(anydb.url, lambda d: apps.start_application(d, a["id"], USER, user_id=ADMIN))
    assert sorted(k for k, _ in out) == ["ok", "refused"], out
    assert anydb.scalar("SELECT COUNT(*) FROM approvals WHERE consumed_at IS NOT NULL") == 1
    assert status(anydb, a["id"]) == "application_started"


def test_unique_key_conflicts_raise_integrity_error(anydb):
    """Expected: duplicate jobs.job_key and a second application for the same job raise IntegrityError."""
    a = prepared(anydb)
    j = anydb.one("SELECT * FROM jobs WHERE id=?", (a["job_id"],))
    with pytest.raises(IntegrityError):
        anydb.insert("INSERT INTO jobs(job_key, employer_id, title, normalized_title, first_discovered_at, last_seen_at)"
                     " VALUES (?,?,?,?,?,?)", (j["job_key"], j["employer_id"], "t", "t", iso(), iso()))
    with pytest.raises(IntegrityError):
        anydb.insert("INSERT INTO applications(job_id, user_id, status, created_at, updated_at) "
                     "VALUES (?, ?, 'shortlisted', ?, ?)",
                     (j["id"], ADMIN, iso(), iso()))


def test_interrupted_discovery(anydb, monkeypatch):
    """Expected: a source crashing mid-run records a partial/failed run with the error, keeps earlier jobs and frees the lock."""
    settings_mod.set_value(anydb, "sources", {"uk_sponsor_register": {"enabled": False}}, user_id=ADMIN)
    ok = FakeSource(jobs=[make_raw(n=1)])
    bad = FakeSource(raise_exc=RuntimeError("connection reset mid-stream"))
    monkeypatch.setattr(agent, "build_sources", lambda h, c: [(ok, "acme"), (bad, "beta")])
    r = agent.run_agent(anydb, agent_config(anydb.url), http=nohttp())
    row = anydb.one("SELECT status, errors_json FROM runs WHERE id=?", (r["run_id"],))
    assert row["status"] in ("partial", "failed") and "connection reset" in row["errors_json"]
    assert anydb.scalar("SELECT COUNT(*) FROM jobs") == 1
    assert anydb.scalar("SELECT COUNT(*) FROM locks") == 0


def test_interrupted_submission_swept(anyurl):
    """Expected: an application_started with no outcome survives a restart and is swept to submission_uncertain."""
    d = open_db(anyurl)
    ensure_admin(d)
    a = started(d)
    d.run("UPDATE applications SET started_at=? WHERE id=?", (iso(utcnow() - timedelta(hours=30)), a["id"]))
    d.close()
    d2 = open_db(anyurl)
    try:
        assert apps.sweep_stale_starts(d2, 24, user_id=ADMIN) == [a["id"]]
        assert status(d2, a["id"]) == "submission_uncertain"
    finally:
        d2.close()


def test_backup_restore(anyurl, tmp_path):
    """Expected: SQLite backup+restore round-trips data; PostgreSQL backup is refused with guidance."""
    d = open_db(anyurl)
    ensure_admin(d)
    upsert_raw(d, make_raw(n=1), settings_mod.get_all(d, ADMIN))
    if d.dialect == "postgres":
        with pytest.raises(DatabaseError, match="PostgreSQL"):
            dbmod.backup(d, tmp_path / "bk")
        d.close()
        return
    bk = dbmod.backup(d, tmp_path / "bk")
    upsert_raw(d, make_raw(n=2, title="Python Developer"), settings_mod.get_all(d, ADMIN))
    assert d.scalar("SELECT COUNT(*) FROM jobs") == 2
    d.close()
    dbmod.restore(bk, Path(d.path))
    d2 = open_db(anyurl)
    try:
        assert d2.scalar("SELECT COUNT(*) FROM jobs") == 1
        assert verify_audit_chain(d2) == (True, None)
    finally:
        d2.close()


def test_audit_chain_valid(anydb):
    """Expected: verify_audit_chain returns (True, None) for an untampered log."""
    for i in range(5):
        audit(anydb, actor="system", entity="job", entity_id=i, action="x", detail={"i": i})
    assert verify_audit_chain(anydb) == (True, None)


def test_audit_tampering_detected(anydb):
    """Expected: after bypassing the trigger and editing a row, verify_audit_chain returns (False, that id)."""
    ids = [audit(anydb, actor="system", entity="job", entity_id=i, action="x") for i in range(3)]
    if anydb.dialect == "sqlite":
        anydb.run("DROP TRIGGER trg_audit_no_update")
    else:
        anydb.run("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_no_update")
    anydb.run("UPDATE audit_events SET actor='attacker' WHERE id=?", (ids[1],))
    assert verify_audit_chain(anydb) == (False, ids[1])


@pytest.mark.parametrize("sql", ["UPDATE audit_events SET actor='x'", "DELETE FROM audit_events"])
def test_audit_append_only(anydb, sql):
    """Expected: UPDATE and DELETE on audit_events are blocked by the append-only trigger."""
    audit(anydb, actor="system", entity="job", entity_id=1, action="x")
    before = anydb.scalar("SELECT COUNT(*) FROM audit_events")
    with pytest.raises(DatabaseError, match="append-only"):
        anydb.run(sql)
    assert anydb.scalar("SELECT COUNT(*) FROM audit_events") == before
