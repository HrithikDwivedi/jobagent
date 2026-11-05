"""C. Application workflow (applications.py state machine), run on SQLite and PostgreSQL."""
from __future__ import annotations

from datetime import timedelta

import pytest

from factories import ADMIN

from jobagent import applications as apps
from jobagent import settings as settings_mod
from jobagent.db import iso, utcnow
from jobagent.settings import SettingsError
from wf import USER, add_job, add_resume, approved, fp, prepared, started, status


def refused(code, fn, *a, **k):
    with pytest.raises(apps.WorkflowError) as ei:
        import inspect
        fn(*a, **(({"user_id": ADMIN} if "user_id" in inspect.signature(fn).parameters else {}) | k))
    assert ei.value.code == code, (ei.value.code, ei.value.message)
    return ei.value


def test_approval_succeeds_with_matching_fingerprint(anydb):
    """Expected: approval with the reviewed fingerprint moves awaiting_approval -> ready_to_apply with one active approval."""
    a = prepared(anydb)
    assert a["status"] == "awaiting_approval"
    out = apps.approve(anydb, a["id"], USER, fp(anydb, a), user_id=ADMIN)
    assert out["status"] == "ready_to_apply" and apps.active_approval(anydb, a["id"], user_id=ADMIN)


def test_rejection_revoke(anydb):
    """Expected: revoking an approval returns the application to awaiting_approval with no active approval."""
    a = approved(anydb)
    out = apps.revoke_approval(anydb, a["id"], USER, user_id=ADMIN)
    assert out["status"] == "awaiting_approval" and apps.active_approval(anydb, a["id"], user_id=ADMIN) is None


def test_expired_approval(anydb):
    """Expected: start with an expired approval raises approval_expired AND persists awaiting_approval + revocation."""
    a = approved(anydb)
    anydb.run("UPDATE approvals SET expires_at=? WHERE application_id=?", (iso(utcnow() - timedelta(hours=1)), a["id"]))
    refused("approval_expired", apps.start_application, anydb, a["id"], USER)
    assert status(anydb, a["id"]) == "awaiting_approval"
    assert apps.active_approval(anydb, a["id"], user_id=ADMIN) is None


def test_approval_with_other_applications_fingerprint(anydb):
    """Expected: approving app A with app B's fingerprint is refused as stale_review and A stays unapproved."""
    a = prepared(anydb, 1, title="Software Engineer")
    b = prepared(anydb, 2, title="Python Developer")
    refused("stale_review", apps.approve, anydb, a["id"], USER, fp(anydb, b))
    assert status(anydb, a["id"]) == "awaiting_approval" and apps.active_approval(anydb, a["id"], user_id=ADMIN) is None


def test_changed_url_after_approval(anydb):
    """Expected: if the apply URL changes after approval, start raises approval_mismatch and the approval is revoked."""
    a = approved(anydb)
    anydb.run("UPDATE jobs SET apply_url='https://boards.greenhouse.io/acme/jobs/999' WHERE id=?", (a["job_id"],))
    refused("approval_mismatch", apps.start_application, anydb, a["id"], USER)
    assert apps.active_approval(anydb, a["id"], user_id=ADMIN) is None
    assert status(anydb, a["id"]) == "awaiting_approval"


def test_missing_resume_blocks_gate(anydb):
    """Expected: an application without a resume cannot be approved (gate: 'No resume selected')."""
    a = prepared(anydb, resume=False)
    e = refused("gate", apps.approve, anydb, a["id"], USER, fp(anydb, a))
    assert "No resume" in e.message


def test_missing_mandatory_answer_blocks_gate(anydb):
    """Expected: a job with unknown country creates a blocking missing item and approval is refused."""
    a = prepared(anydb, location_raw=None)
    assert any(m["item"] == "country" and m["blocking"] for m in apps.loads(a["missing_info_json"], []))
    e = refused("gate", apps.approve, anydb, a["id"], USER, fp(anydb, a))
    assert "Missing required information" in e.message


@pytest.mark.parametrize("field,value,msg", [("email", "not-an-email", "email"), ("phone", "call me maybe", "phone"),
                                             ("email", "a@b", "email")])
def test_invalid_email_phone_rejected(field, value, msg):
    """Expected: invalid candidate email/phone values are rejected by settings validation."""
    cand = dict(settings_mod.DEFAULT_SETTINGS["candidate"], **{field: value})
    with pytest.raises(SettingsError) as ei:
        settings_mod.validate("candidate", cand)
    assert msg in str(ei.value).lower()


def test_non_user_actor_refused(anydb):
    """Expected: approve/start by actor 'agent' is refused with not_user."""
    a = prepared(anydb)
    refused("not_user", apps.approve, anydb, a["id"], "agent", fp(anydb, a))
    b = approved(anydb, 2, title="Python Developer")
    refused("not_user", apps.start_application, anydb, b["id"], "agent")


def test_job_closed_cannot_approve_or_start(anydb):
    """Expected: a closed job blocks approval (gate) and blocks start of an existing approval (job_not_open)."""
    a = prepared(anydb)
    anydb.run("UPDATE jobs SET status='closed' WHERE id=?", (a["job_id"],))
    e = refused("gate", apps.approve, anydb, a["id"], USER, fp(anydb, a))
    assert "closed" in e.message
    b = approved(anydb, 2, title="Python Developer")
    anydb.run("UPDATE jobs SET status='closed' WHERE id=?", (b["job_id"],))
    refused("job_not_open", apps.start_application, anydb, b["id"], USER)
    assert status(anydb, b["id"]) == "ready_to_apply"


def test_manual_portal_never_automated(anydb):
    """Expected: start only returns the destination URL and sets application_started; nothing is recorded as submitted."""
    a = approved(anydb)
    res = apps.start_application(anydb, a["id"], USER, user_id=ADMIN)
    assert res["destination_url"].startswith("https://boards.greenhouse.io/")
    app = res["application"]
    assert app["status"] == "application_started" and app["submitted_at"] is None
    assert app["submission_evidence_json"] is None
    assert anydb.scalar("SELECT COUNT(*) FROM audit_events WHERE to_status='submitted'") == 0


def test_network_failure_before_submit_retry_needs_new_approval(anydb):
    """Expected: failed/before_submit -> submission_failed; retry allowed -> awaiting_approval; old approval unusable."""
    a = started(anydb)
    out = apps.record_outcome(anydb, a["id"], "failed", USER, failure_stage="before_submit", user_id=ADMIN)
    assert out["status"] == "submission_failed"
    out = apps.retry_after_failure(anydb, a["id"], USER, user_id=ADMIN)
    assert out["status"] == "awaiting_approval" and apps.active_approval(anydb, a["id"], user_id=ADMIN) is None
    refused("not_ready", apps.start_application, anydb, a["id"], USER)


def test_timeout_after_submit_is_uncertain_no_retry(anydb):
    """Expected: failed/after_submit_unknown -> submission_uncertain; retry_after_failure is refused."""
    a = started(anydb)
    out = apps.record_outcome(anydb, a["id"], "failed", USER, failure_stage="after_submit_unknown", user_id=ADMIN)
    assert out["status"] == "submission_uncertain"
    refused("not_failed", apps.retry_after_failure, anydb, a["id"], USER)
    refused("not_ready", apps.start_application, anydb, a["id"], USER)


def test_duplicate_start_refused(anydb):
    """Expected: a second start of the same application is refused (approval consumed / not_ready)."""
    a = approved(anydb)
    apps.start_application(anydb, a["id"], USER, user_id=ADMIN)
    refused("not_ready", apps.start_application, anydb, a["id"], USER)
    assert anydb.scalar("SELECT COUNT(*) FROM approvals WHERE application_id=? AND consumed_at IS NOT NULL",
                        (a["id"],)) == 1


def test_uncertain_never_auto_resubmitted(anydb):
    """Expected: sweep_stale_starts only moves stale application_started -> submission_uncertain; nothing else changes."""
    a = started(anydb)
    other = prepared(anydb, 2, title="Python Developer")
    anydb.run("UPDATE applications SET started_at=? WHERE id=?", (iso(utcnow() - timedelta(hours=48)), a["id"]))
    approvals_before = anydb.all("SELECT * FROM approvals ORDER BY id")
    assert apps.sweep_stale_starts(anydb, 24, user_id=ADMIN) == [a["id"]]
    assert status(anydb, a["id"]) == "submission_uncertain"
    assert status(anydb, other["id"]) == "awaiting_approval"
    assert anydb.all("SELECT * FROM approvals ORDER BY id") == approvals_before
    assert apps.sweep_stale_starts(anydb, 24, user_id=ADMIN) == []  # second sweep is a no-op
    assert anydb.scalar("SELECT COUNT(*) FROM applications WHERE status IN ('application_started','submitted')") == 0


def test_submitted_without_evidence_refused(anydb):
    """Expected: 'submitted' without reference/confirmation/screenshot is refused with no_evidence."""
    a = started(anydb)
    refused("no_evidence", apps.record_outcome, anydb, a["id"], "submitted", USER, confirmation_text="ok")
    assert status(anydb, a["id"]) == "application_started"


def test_submitted_with_reference_accepted(anydb):
    """Expected: 'submitted' with a reference number is accepted and the evidence stored."""
    a = started(anydb)
    out = apps.record_outcome(anydb, a["id"], "submitted", USER, reference_number="GH-12345", user_id=ADMIN)
    assert out["status"] == "submitted" and out["reference_number"] == "GH-12345"
    assert "GH-12345" in out["submission_evidence_json"]


@pytest.mark.parametrize("url,code", [
    ("http://boards.greenhouse.io/acme/jobs/1", "insecure_destination"),
    ("https://evil.example.com/apply", "unverified_destination"),
    ("https://127.0.0.1/apply", "unverified_destination"),
    (None, "no_destination"),
])
def test_destination_unverified_blocks(anydb, url, code):
    """Expected: http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate."""
    a = prepared(anydb)
    anydb.run("UPDATE jobs SET apply_url=? WHERE id=?", (url, a["job_id"]))
    job = anydb.one("SELECT * FROM jobs WHERE id=?", (a["job_id"],))
    refused(code, apps.verify_destination, url, job, anydb)
    refused("gate", apps.approve, anydb, a["id"], USER, fp(anydb, a))


def test_user_cancellation_withdraw(anydb):
    """Expected: user withdrawal moves an awaiting application to 'withdrawn' (terminal)."""
    a = approved(anydb)
    out = apps.update_progress(anydb, a["id"], USER, "withdrawn", user_id=ADMIN)
    assert out["status"] == "withdrawn"
    refused("invalid_transition", apps.update_progress, anydb, a["id"], USER, "interview")


def test_mark_applied_externally(anydb):
    """Expected: external application -> submitted+external=1 with declared evidence; later prepare refused."""
    jid = add_job(anydb)
    out = apps.mark_applied_externally(anydb, jid, USER, "2026-10-01", "Applied on company site", user_id=ADMIN)
    assert out["status"] == "submitted" and out["external"] == 1
    assert "user_declared_external" in out["submission_evidence_json"]
    refused("not_preparable", apps.prepare, anydb, jid, "agent", add_resume(anydb), "x", "template")
    refused("bad_date", apps.mark_applied_externally, anydb, add_job(anydb, 2, title="Python Developer"), USER,
            "01/10/2026", "")


def test_equivalent_vacancy_already_submitted(anydb):
    """Expected: get_or_create for an equivalent vacancy (same employer/title/country) raises already_applied."""
    a = started(anydb)
    apps.record_outcome(anydb, a["id"], "submitted", USER, reference_number="R1", user_id=ADMIN)
    j = anydb.one("SELECT * FROM jobs WHERE id=?", (a["job_id"],))
    jid2 = anydb.insert("INSERT INTO jobs(job_key, employer_id, title, normalized_title, country, first_discovered_at, "
                        "last_seen_at, status, apply_url) VALUES ('dup-key', ?, ?, ?, ?, ?, ?, 'open', ?)",
                        (j["employer_id"], j["title"], j["normalized_title"], j["country"], iso(), iso(),
                         "https://boards.greenhouse.io/acme/jobs/555"))
    refused("already_applied", apps.get_or_create, anydb, jid2, "agent")


def test_manual_correction_cannot_set_submitted(anydb):
    """Expected: correct_status/update_progress refuse to fabricate 'submitted'."""
    a = prepared(anydb)
    refused("not_allowed", apps.correct_status, anydb, a["id"], USER, "submitted", "I did it honestly")
    refused("no_evidence", apps.update_progress, anydb, a["id"], USER, "submitted")
    assert status(anydb, a["id"]) == "awaiting_approval"


def test_daily_limit_enforced(anydb):
    """Expected: once max_applications_per_day starts are reached, further approvals are blocked by the gate."""
    settings_mod.set_value(anydb, "max_applications_per_day", 1, user_id=ADMIN)
    started(anydb, 1, title="Software Engineer")
    b = prepared(anydb, 2, title="Python Developer")
    e = refused("gate", apps.approve, anydb, b["id"], USER, fp(anydb, b))
    assert "Daily application limit" in e.message


def test_stale_listing_blocks_approval(anydb):
    """Expected: a listing not verified within verification_max_age_hours cannot be approved."""
    a = prepared(anydb)
    anydb.run("UPDATE jobs SET last_verified_at=? WHERE id=?", (iso(utcnow() - timedelta(days=5)), a["job_id"]))
    e = refused("gate", apps.approve, anydb, a["id"], USER, fp(anydb, a))
    assert "verified" in e.message
