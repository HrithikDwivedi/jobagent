"""Command-line entry point:  python -m jobagent [--user USERNAME] <command>

Commands act on ONE user's private job hunt: --user USERNAME (default: the administrator).
  init                 create/migrate the database and validate configuration
  create-user          first-run administrator account creation (interactive, password never echoed)
  reset-password       local account recovery (requires access to this machine / database)
  users                list accounts (status, role, counts)
  approve-user NAME    approve a pending sign-up (also: reject-user / suspend-user / reactivate-user NAME)
  import-resume [PATH] import a resume (default: JOBAGENT_RESUME_PATH); never modifies the file
  analyze-resume       print the canonical resume analysis
  discover             fetch & deduplicate jobs (installation-wide), assess and score for every approved user
  rank                 re-score open jobs with current resume/settings (the selected user, or --all-users)
  prepare              prepare application drafts for top matches (stops at approval)
  run                  full agent run (discover once + score/prepare for every approved user), same as the scheduler
  review               list applications awaiting your approval
  approve APP_ID       approve one application after reviewing exactly what will be sent
  apply APP_ID         start an approved application (prints the official URL to submit on)
  autoapply            auto-apply worker for policy-approved applications [--dry-run] [--headed] [--limit N]
  refresh-status       re-verify listings for active applications and flag stale starts as uncertain
  summary              daily summary
  serve                start the dashboard on http://127.0.0.1:8765
  backup / restore F   SQLite backup / restore
  test                 run the automated test suite"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from . import agent as agent_mod
from . import applications as apps
from . import settings as settings_mod
from . import users as users_mod
from .ai import make_provider
from .config import PROJECT_ROOT, load_config, validate_config
from .db import Database, DatabaseError, backup, iso, loads, open_db, restore, utcnow
from .http import HttpClient
from .resume import store as resume_store
from .resume.parser import ResumeError
from .web import security


def _db(cfg) -> Database:
    try:
        return open_db(cfg.database_url)
    except DatabaseError as e:
        sys.exit(f"Database error: {e}")


def _print_run(res: dict) -> None:
    print(f"Run {res['run_id']}: {res['status']}")
    for k, v in (res.get("stats") or {}).items():
        if v:
            print(f"  {k:22} {v}")
    for e in res.get("errors", [])[:10]:
        print(f"  ! {e}")


def resolve_user(db: Database, username: str | None) -> dict:
    """The --user account (default: the administrator). Exits if it does not exist."""
    if username:
        u = users_mod.by_username(db, username)
        if not u:
            sys.exit(f"No such user '{username}'.")
        return u
    aid = users_mod.admin_id(db)
    if aid is None:
        sys.exit("Create an account first (create-user).")
    return users_mod.get_user(db, aid)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jobagent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", default=None, help="act on this user's data (default: the administrator)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "create-user", "reset-password", "users", "analyze-resume", "discover", "prepare", "run",
                 "review", "refresh-status", "summary", "serve", "backup", "test"):
        sub.add_parser(name)
    sp = sub.add_parser("rank")
    sp.add_argument("--all-users", action="store_true", help="re-score for every approved user")
    for name in ("approve-user", "reject-user", "suspend-user", "reactivate-user"):
        s = sub.add_parser(name)
        s.add_argument("username")
    sp = sub.add_parser("import-resume")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--make-canonical", action="store_true", help="replace the canonical resume (asks to confirm)")
    for name in ("approve", "apply"):
        s = sub.add_parser(name)
        s.add_argument("app_id", type=int)
    s = sub.add_parser("restore")
    s.add_argument("file")
    s = sub.add_parser("autoapply", help="submit policy-approved applications on the employer's form")
    s.add_argument("--dry-run", action="store_true", help="fill in a throwaway browser; never submit or change state")
    s.add_argument("--headed", action="store_true", help="visible browser (needed for fill-only / CAPTCHA hand-off)")
    s.add_argument("--limit", type=int, default=None, help="at most N applications this run")
    args = p.parse_args(argv)
    cfg = load_config()

    if args.cmd == "test":
        return subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=PROJECT_ROOT)

    if args.cmd == "serve":
        problems = validate_config(cfg)
        if problems:
            sys.exit("Configuration problems:\n- " + "\n- ".join(problems))
        import uvicorn
        from .web.app import create_app
        _db(cfg).close()
        print(f"JobAgent dashboard: http://{cfg.host}:{cfg.port}  (Ctrl+C to stop)")
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", proxy_headers=False)
        return 0

    db = _db(cfg)
    if args.cmd == "init":
        problems = validate_config(cfg)
        print("Database ready:", cfg.database_url.split("://")[0] if "://" in cfg.database_url else "sqlite")
        print("Config OK" if not problems else "Config problems:\n- " + "\n- ".join(problems))
        if security.user_count(db) == 0:
            print("No account yet: run `python -m jobagent create-user` (or open /setup on localhost).")
        return 1 if problems else 0

    if args.cmd == "create-user":
        if security.user_count(db) > 0:
            sys.exit("An account already exists. New users sign up on /signup and are approved by the administrator; "
                     "use reset-password to recover access.")
        username = input("Username: ").strip()
        email = input("Email (optional): ").strip() or cfg.admin_email
        pw = getpass.getpass("Password (12+ chars, upper & lower case, digit): ")
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
        try:
            security.create_user(db, username, pw, actor="cli", email=email)
        except security.AuthError as e:
            sys.exit(str(e))
        print("Administrator account created.")
        return 0

    if args.cmd == "reset-password":
        username = input("Username: ").strip()
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords do not match.")
        try:
            security.set_password(db, username, pw, actor="cli")
        except security.AuthError as e:
            sys.exit(str(e))
        print("Password reset; all sessions were signed out.")
        return 0

    if args.cmd == "users":
        for u in users_mod.list_users(db):
            print(f"#{u['id']:<4} {u['username']:20} {u['role']:6} {u['status']:10} {u['email'] or '-':30} "
                  f"apps={u['applications']} resumes={u['resumes']}")
        return 0

    if args.cmd in ("approve-user", "reject-user", "suspend-user", "reactivate-user"):
        target = users_mod.by_username(db, args.username)
        if not target:
            sys.exit(f"No such user '{args.username}'.")
        action = args.cmd.split("-")[0]
        try:
            u = users_mod.change_status(db, target["id"], action, actor="cli", actor_id=users_mod.admin_id(db))
        except users_mod.AccountError as e:
            sys.exit(str(e))
        if action in ("approve", "reject"):
            from .config import acceptable_base_url
            from .mailer import make_mailer
            users_mod.send_decision(db, make_mailer(cfg), base_url=acceptable_base_url(cfg.public_url), user=u,
                                    action=action)
        print(f"{u['username']}: {u['status']}")
        return 0

    me = resolve_user(db, args.user) if args.cmd not in ("backup", "restore") else None
    uid = int(me["id"]) if me else 0
    actor_name = f"user:{me['username']}" if me else "cli"

    if args.cmd == "import-resume":
        if me["role"] != "admin" and not args.path:
            sys.exit("Give the resume path explicitly for a non-admin user.")
        try:
            v = resume_store.import_original(db, args.path or cfg.resume_path, actor="cli",
                                             make_canonical_if_none=True, user_id=uid)
        except ResumeError as e:
            print(f"Resume error ({e.code}): {e.message}")
            for c in e.candidates:
                print("   candidate:", c)
            return 2
        print(f"Imported resume version {v['id']} ({v['label']}) for {me['username']}; "
              f"canonical={bool(v['is_canonical'])}")
        if args.make_canonical and not v["is_canonical"]:
            if input("Replace the canonical resume with this one? Type 'yes': ").strip() == "yes":
                resume_store.set_canonical(db, v["id"], "cli", True, user_id=uid)
                print("Canonical resume updated.")
        return 0

    if args.cmd == "analyze-resume":
        r = resume_store.canonical(db, user_id=uid)
        if not r:
            sys.exit("No canonical resume. Run import-resume first.")
        r = resume_store.reanalyze(db, r["id"], "cli", user_id=uid)
        a = r["analysis"]
        print(f"Resume score {a['score']}/100 | experience {a['experience_months']} months | "
              f"{int(a['metric_ratio'] * 100)}% of bullets have metrics")
        for i in a["issues"]:
            print(f"  [{i['severity']:6}] {i['category']:11} {i['message']}")
            if i.get("suggestion"):
                print(f"           -> {i['suggestion']}")
        return 0

    if args.cmd in ("discover", "rank", "prepare", "run"):
        flags = {"discover": dict(discover=True, prepare=False), "rank": dict(discover=False, prepare=False),
                 "prepare": dict(discover=False, prepare=True), "run": dict(discover=True, prepare=True)}[args.cmd]
        if args.cmd in ("rank", "prepare") and not getattr(args, "all_users", False):
            flags["user_ids"] = [uid]
        res = agent_mod.run_agent(db, cfg, trigger="cli", provider=make_provider(cfg), **flags)
        _print_run(res)
        return 0 if res["status"] in ("succeeded", "partial") else 1

    if args.cmd == "review":
        rows = db.all("SELECT a.id, a.status, j.title, e.name AS employer, j.country FROM applications a JOIN jobs j ON "
                      "j.id=a.job_id JOIN employers e ON e.id=j.employer_id WHERE a.user_id=? AND a.status IN "
                      "('awaiting_approval','ready_to_apply','submission_uncertain') ORDER BY a.id", (uid,))
        if not rows:
            print("Nothing awaiting review.")
        s = settings_mod.get_all(db, uid)
        for r in rows:
            a = apps.get_app(db, r["id"], user_id=uid)
            gate = apps.approval_gate_checks(db, a, s) if r["status"] == "awaiting_approval" else []
            print(f"#{r['id']:<4} {apps.STATUS_LABELS[r['status']]:22} {r['employer']} — {r['title']} ({r['country']})")
            for g in gate:
                print(f"       blocked: {g}")
        print("\nReview details in the dashboard (Applications) or with `approve <id>`.")
        return 0

    if args.cmd == "approve":
        a = apps.get_app(db, args.app_id, user_id=uid)
        if not a:
            sys.exit("Application not found.")
        snap = apps.snapshot(db, a)
        cl = db.one("SELECT body FROM cover_letters WHERE id=? AND user_id=?", (a["cover_letter_id"], uid)) \
            if a["cover_letter_id"] else None
        print(json.dumps({k: v for k, v in snap.items() if k not in ("resume_sha256", "cover_letter_sha256")},
                         indent=2, ensure_ascii=False))
        if cl:
            print("\n--- Cover letter ---\n" + cl["body"] + "\n--------------------")
        problems = apps.approval_gate_checks(db, a, settings_mod.get_all(db, uid))
        if problems:
            sys.exit("Cannot approve: " + " ".join(problems))
        if input("Approve THIS application exactly as shown? Type 'approve': ").strip() != "approve":
            print("Not approved.")
            return 1
        try:
            apps.approve(db, a["id"], actor_name, apps.fingerprint(snap), user_id=uid)
        except apps.WorkflowError as e:
            sys.exit(e.message)
        print("Approved. Run `apply", a["id"], "` when you are ready to submit on the employer's site.")
        return 0

    if args.cmd == "apply":
        try:
            res = apps.start_application(db, args.app_id, actor_name, user_id=uid)
        except apps.WorkflowError as e:
            sys.exit(e.message)
        print("Open and submit on the official page:\n  " + res["destination_url"])
        print("Then record the confirmation in the dashboard (Applications -> this application).")
        return 0

    if args.cmd == "autoapply":
        from .autoapply import run_autoapply
        try:
            res = run_autoapply(db, cfg, dry_run=args.dry_run, headless=False if args.headed else None,
                                limit=args.limit, trigger="cli", user_id=uid if args.user else None)
        except ImportError:
            sys.exit("Playwright is not installed: pip install -r requirements-worker.txt && "
                     "python -m playwright install chromium")
        print(f"Auto-apply: {res['status']}" + (f" (run {res['run_id']})" if res.get("run_id") else "")
              + (" [dry run]" if args.dry_run else ""))
        if res.get("message"):
            print("  " + res["message"])
        for k, v in (res.get("stats") or {}).items():
            if v:
                print(f"  {k:22} {v}")
        for r in res.get("results", []):
            print(f"  #{r['application_id']}: {r.get('outcome')} {r.get('reason') or ''}".rstrip())
            for q in r.get("unknown_questions") or []:
                print(f"       needs you: {q}")
            if r.get("screenshot"):
                print(f"       screenshot: {r['screenshot']}")
        return 0 if res["status"] in ("succeeded", "partial", "disabled") else 1

    if args.cmd == "refresh-status":
        http = HttpClient(user_agent=cfg.user_agent)
        try:
            ids = [r["job_id"] for r in db.all("SELECT job_id FROM applications WHERE user_id=? AND status IN "
                                               "('shortlisted','awaiting_approval','ready_to_apply')", (uid,))]
            for jid in ids:
                print(f"job {jid}: {agent_mod.verify_job(db, jid, http)}")
        finally:
            http.close()
        s = settings_mod.get_all(db, uid)
        moved = apps.sweep_stale_starts(db, s["uncertain_after_hours"], user_id=uid)
        print(f"{len(moved)} started application(s) marked 'submission uncertain' (check your email/portal).")
        return 0

    if args.cmd == "summary":
        since = iso(utcnow() - timedelta(days=1))
        new = db.scalar("SELECT COUNT(*) FROM jobs WHERE first_discovered_at >= ? AND is_sample=0", (since,))
        rows = db.all("SELECT status, COUNT(*) AS n FROM applications WHERE is_sample=0 AND user_id=? GROUP BY status",
                      (uid,))
        last = db.one("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
        print(f"Daily summary for {me['username']} ({iso()[:10]})")
        print(f"  New jobs in last 24h: {new}")
        for r in rows:
            print(f"  {apps.STATUS_LABELS[r['status']]:24} {r['n']}")
        if last and me["role"] == "admin":
            print(f"  Last run: {last['started_at']} {last['status']} errors={len(loads(last['errors_json'], []))}")
        top = db.all("SELECT j.id, j.title, e.name AS employer, sc.score FROM jobs j JOIN employers e ON "
                     "e.id=j.employer_id JOIN job_scores sc ON sc.id=(SELECT MAX(id) FROM job_scores WHERE job_id=j.id "
                     "AND user_id=?) WHERE j.status='open' AND j.is_sample=0 AND sc.recommendation='apply' "
                     "ORDER BY sc.score DESC LIMIT 5", (uid,))
        for t in top:
            print(f"  ★ {t['score']:5.1f}  {t['employer']} — {t['title']} (job {t['id']})")
        return 0

    if args.cmd == "backup":
        try:
            print("Backup written:", backup(db, cfg.backups_dir))
        except DatabaseError as e:
            sys.exit(str(e))
        return 0

    if args.cmd == "restore":
        if db.dialect != "sqlite":
            sys.exit("Restore is for SQLite; use your PostgreSQL provider's restore for hosted databases.")
        path = Path(db.path)
        db.close()
        if input(f"Overwrite {path} with {args.file}? Stop the dashboard first. Type 'restore': ").strip() != "restore":
            return 1
        restore(Path(args.file), path)
        print("Restored.")
        return 0
    return 0


def security_user(db: Database) -> str:
    """Username of the administrator (kept for backwards compatibility)."""
    return resolve_user(db, None)["username"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
