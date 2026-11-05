"""Workflow helpers (SYNTHETIC jobs/resumes inserted directly)."""
from __future__ import annotations

from factories import ADMIN, make_raw
from jobagent import applications as apps
from jobagent import settings as settings_mod
from jobagent.db import iso
from jobagent.ingest import upsert_raw

USER = "user:alex"
U = {"user_id": ADMIN}   # keyword for the admin's private data: apps.approve(..., **U)


def add_resume(db, user_id=ADMIN) -> int:
    rid = db.scalar("SELECT id FROM resume_versions WHERE label='synthetic' AND user_id=?", (user_id,))
    if rid:
        return rid
    return db.insert("INSERT INTO resume_versions(label, kind, is_canonical, structured_json, method, status, created_at,"
                     " user_id) VALUES ('synthetic','original',1,'{\"text\":\"synthetic\"}','parse','approved',?,?)",
                     (iso(), user_id))


def add_job(db, n=1, **kw) -> int:
    d, jid, why = upsert_raw(db, make_raw(n=n, **kw), settings_mod.get_all(db, ADMIN))
    assert d == "new", (d, why)
    return jid


def prepared(db, n=1, resume=True, user_id=ADMIN, **kw) -> dict:
    jid = add_job(db, n, **kw)
    rid = add_resume(db, user_id) if resume else None
    return apps.prepare(db, jid, "agent", rid, "Dear Hiring Team, synthetic letter.", "template", user_id=user_id)


def fp(db, app) -> str:
    return apps.fingerprint(apps.snapshot(db, apps._get_app(db, app["id"], app["user_id"])))


def approved(db, n=1, **kw) -> dict:
    a = prepared(db, n, **kw)
    return apps.approve(db, a["id"], USER, fp(db, a), user_id=a["user_id"])


def started(db, n=1, **kw) -> dict:
    a = approved(db, n, **kw)
    return apps.start_application(db, a["id"], USER, user_id=a["user_id"])["application"]


def status(db, app_id) -> str:
    return db.scalar("SELECT status FROM applications WHERE id=?", (app_id,))
