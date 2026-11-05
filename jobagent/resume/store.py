"""Persisted resume versions. The original file is never written; its bytes are stored as a snapshot blob.
Variant PDF/DOCX files are generated on demand from the stored structured data + persisted one-page layout
(no disk state needed).

Multi-user: every resume version and file belongs to exactly one user. All functions take `user_id` and only ever
read or write that user's rows; each user has their own canonical resume."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from .. import jd as jd_mod
from .. import settings as settings_mod
from ..db import Database, audit, dumps, iso, loads
from .analysis import analyze
from .export import export_docx, export_pdf
from .parser import SUPPORTED, ResumeError, discover_resume, parse, sha256_file
from .tailor import TailorResult, fingerprint, tailor
from .variants import ROLE_FAMILIES, build_variant, verify_no_new_facts  # noqa: F401  (build_variant: legacy API)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {".pdf": "application/pdf",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 ".txt": "text/plain"}


def import_original(db: Database, path: Path | str, actor: str = "system",
                    make_canonical_if_none: bool = True, *, user_id: int) -> dict:
    """Import from a local path (CLI / local mode). The file is opened read-only."""
    p = discover_resume(path)
    before = sha256_file(p)
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != before:
        raise ResumeError("modified", "Resume file changed while being read; try again.")
    return import_bytes(db, data, p.name, actor=actor, make_canonical_if_none=make_canonical_if_none,
                        source_path=str(p), user_id=user_id)


def import_bytes(db: Database, data: bytes, filename: str, actor: str = "system",
                 make_canonical_if_none: bool = True, source_path: str | None = None, *, user_id: int) -> dict:
    """Import an uploaded resume. Validates type by extension AND magic bytes, size, and parseability."""
    name = Path(filename).name  # strip any client-supplied directories (path traversal)
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        raise ResumeError("unsupported_format", f"'{name}': only PDF, DOCX or TXT resumes are accepted.")
    if not data:
        raise ResumeError("empty", f"'{name}' is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ResumeError("too_large", "Resume files must be under 5 MB.")
    if suffix == ".pdf" and not data.startswith(b"%PDF"):
        raise ResumeError("corrupted", f"'{name}' does not look like a PDF file.")
    if suffix == ".docx" and not data.startswith(b"PK"):
        raise ResumeError("corrupted", f"'{name}' does not look like a Word .docx file.")
    sha = hashlib.sha256(data).hexdigest()
    existing = db.one("SELECT id FROM resume_versions WHERE kind='original' AND file_sha256=? AND user_id=?",
                      (sha, user_id))
    if existing:
        return get(db, existing["id"], user_id=user_id)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f"resume{suffix}"
        tmp_path.write_bytes(data)
        structured = parse(tmp_path)
    structured["source_file"] = name
    s = settings_mod.get_all(db, user_id)
    analysis = analyze(structured, s["candidate"], settings_mod.all_target_countries(s))
    with db.transaction():
        file_id = db.insert(
            "INSERT INTO files(kind, original_name, content_type, size_bytes, sha256, data, created_at, user_id) "
            "VALUES ('resume_original', ?, ?, ?, ?, ?, ?, ?)",
            (name, CONTENT_TYPES[suffix], len(data), sha, db.blob(data), iso(), user_id))
        has_canonical = db.one("SELECT 1 AS x FROM resume_versions WHERE is_canonical=1 AND user_id=?", (user_id,))
        canonical_flag = 1 if (make_canonical_if_none and not has_canonical) else 0
        rid = db.insert(
            """INSERT INTO resume_versions(label, kind, is_canonical, source_path, source_file_id, file_sha256,
                   structured_json, analysis_json, method, status, created_at, user_id)
               VALUES (?, 'original', ?, ?, ?, ?, ?, ?, 'parse', 'approved', ?, ?)""",
            (f"Original: {name}", canonical_flag, source_path, file_id, sha, dumps(structured), dumps(analysis),
             iso(), user_id))
        audit(db, actor=actor, entity="resume", entity_id=rid, action="import_original",
              detail={"file": name, "sha256": sha, "canonical": bool(canonical_flag)})
    return get(db, rid, user_id=user_id)


def get(db: Database, rid: int, *, user_id: int) -> dict | None:
    """The version if it belongs to `user_id`, else None (callers turn that into 404)."""
    r = db.one("SELECT * FROM resume_versions WHERE id=? AND user_id=?", (rid, user_id))
    if r:
        r["structured"] = loads(r["structured_json"], {})
        r["analysis"] = loads(r["analysis_json"], {})
        r["changes"] = loads(r["changes_json"], [])
        r["layout"] = loads(r.get("layout_json"), None)
        r["style"] = loads(r.get("style_json"), {}) or {}
    return r


def original_file(db: Database, rid: int, *, user_id: int) -> dict | None:
    return db.one("SELECT f.* FROM files f JOIN resume_versions r ON r.source_file_id = f.id "
                  "WHERE r.id=? AND r.user_id=? AND f.user_id=?", (rid, user_id, user_id))


def canonical(db: Database, *, user_id: int) -> dict | None:
    row = db.one("SELECT id FROM resume_versions WHERE is_canonical=1 AND user_id=?", (user_id,))
    return get(db, row["id"], user_id=user_id) if row else None


def list_versions(db: Database, *, user_id: int) -> list[dict]:
    return db.all("SELECT id, label, kind, parent_id, is_canonical, role_family, target_country, job_id, status, "
                  "method, created_at FROM resume_versions WHERE user_id=? ORDER BY id DESC", (user_id,))


def reanalyze(db: Database, rid: int, actor: str = "system", *, user_id: int) -> dict:
    """Re-run analysis (e.g. after the user updates confirmed facts in Settings)."""
    v = get(db, rid, user_id=user_id)
    if not v or v["kind"] != "original":
        raise LookupError("Original resume not found")
    s = settings_mod.get_all(db, user_id)
    analysis = analyze(v["structured"], s["candidate"], settings_mod.all_target_countries(s))
    with db.transaction():
        db.run("UPDATE resume_versions SET analysis_json=? WHERE id=? AND user_id=?", (dumps(analysis), rid, user_id))
    return get(db, rid, user_id=user_id)


def set_canonical(db: Database, rid: int, actor: str, confirm: bool, *, user_id: int) -> None:
    """Replacing the canonical resume is the user's decision; requires explicit confirmation."""
    if not confirm:
        raise PermissionError("Changing the canonical resume requires explicit confirmation.")
    v = get(db, rid, user_id=user_id)
    if not v:
        raise LookupError("Resume version not found")
    if v["kind"] != "original":
        raise ValueError("Only an original (parsed) resume can be canonical; variants are derived from it.")
    with db.transaction():
        old = db.one("SELECT id FROM resume_versions WHERE is_canonical=1 AND user_id=?", (user_id,))
        db.run("UPDATE resume_versions SET is_canonical=0 WHERE is_canonical=1 AND user_id=?", (user_id,))
        db.run("UPDATE resume_versions SET is_canonical=1 WHERE id=? AND user_id=?", (rid, user_id))
        audit(db, actor=actor, entity="resume", entity_id=rid, action="set_canonical",
              detail={"previous": old["id"] if old else None})


def create_edited_canonical(db: Database, structured: dict, actor: str, reason: str, confirm: bool, *,
                            user_id: int) -> dict:
    """Store user-confirmed edited master content as a NEW original version (method='manual_edit',
    parent_id = previous canonical) and make it canonical. No file on disk is read or written."""
    if not confirm:
        raise PermissionError("Editing the canonical resume requires explicit confirmation.")
    from .edits import refresh_derived
    reason = " ".join((reason or "").split())[:300] or "Edited by user"
    old = canonical(db, user_id=user_id)
    st = refresh_derived(dict(structured))
    s = settings_mod.get_all(db, user_id)
    analysis = analyze(st, s["candidate"], settings_mod.all_target_countries(s))
    with db.transaction():
        rid = db.insert(
            """INSERT INTO resume_versions(label, kind, parent_id, is_canonical, source_path, source_file_id,
                   file_sha256, structured_json, analysis_json, method, status, created_at, user_id)
               VALUES (?, 'original', ?, 0, NULL, NULL, NULL, ?, ?, 'manual_edit', 'approved', ?, ?)""",
            (f"Edited: {reason[:120]}", old["id"] if old else None, dumps(st), dumps(analysis), iso(), user_id))
        db.run("UPDATE resume_versions SET is_canonical=0 WHERE is_canonical=1 AND user_id=?", (user_id,))
        db.run("UPDATE resume_versions SET is_canonical=1 WHERE id=? AND user_id=?", (rid, user_id))
        audit(db, actor=actor, entity="resume", entity_id=rid, action="edit_canonical",
              detail={"previous": old["id"] if old else None, "reason": reason})
    return get(db, rid, user_id=user_id)


def edit_canonical_from_file(db: Database, json_path: Path | str, actor: str, *, user_id: int) -> dict:
    """Load an edited structured resume (JSON) and store it as the new canonical (explicit user action)."""
    import json
    p = Path(json_path)
    return create_edited_canonical(db, json.loads(p.read_text(encoding="utf-8")), actor,
                                   f"Edited from {p.name}", confirm=True, user_id=user_id)


# ---------------------------------------------------------------- tailored variants

def _guard(base: dict, result: TailorResult, candidate: dict) -> None:
    problems = verify_no_new_facts(base["structured"], result.structured, candidate,
                                   result.style.get("guard_allowed_text", ""), check_proper_nouns=True)
    if problems:
        raise ResumeError("fact_guard", "Variant rejected because it introduced new facts: " + "; ".join(problems))


def _style_record(result: TailorResult, fp: str) -> dict:
    keep = ("company_type", "company_type_source", "company_type_evidence", "company_label", "country",
            "country_name", "spelling", "date_format", "page_size", "doc_title", "headings", "one_page",
            "ai_rewrite", "role_family", "role_family_label", "bullet_max_chars", "country_notes",
            "guard_allowed_text", "styles_version", "styles_hash", "tailor_version", "company_profile", "ats",
            "coverage")
    out = {k: result.style.get(k) for k in keep}
    out.update({"fingerprint": fp, "warnings": result.warnings[:40], "method": result.method})
    return out


def _use_ai(ai, prefs: dict, user_id: int | None = None) -> bool:
    if ai is None or not getattr(ai, "available", False) or not prefs.get("ai_rewrite", True):
        return False
    # AI is an administrator-only feature; an AIService built for another user is never used here.
    # (AIService itself refuses non-admin users; this additionally stops one user's service being used for another.)
    owner = getattr(ai, "user_id", None)
    return owner is None or owner == user_id


def create_variant(db: Database, role_family: str, country: str | None, actor: str = "system",
                   ai=None, *, user_id: int) -> dict:
    """Generic (no specific job) role-family/country variant: tailored, one page, fact-guarded."""
    base = canonical(db, user_id=user_id)
    if not base:
        raise ResumeError("no_canonical", "No canonical resume imported yet.")
    if role_family not in ROLE_FAMILIES:
        raise ValueError("Unknown role family")
    if country and country not in settings_mod.COUNTRY_NAMES:
        raise ValueError("Unknown country")
    s = settings_mod.get_all(db, user_id)
    prefs, cand = s["resume_style"], s["candidate"]
    use_ai = _use_ai(ai, prefs, user_id)
    result = tailor(base["structured"], base["analysis"], cand, prefs, role_family=role_family, country=country,
                    ai=ai if use_ai else None)
    _guard(base, result, cand)
    fp = fingerprint(base["id"], base.get("file_sha256"), role_family, country, prefs, cand, use_ai)
    existing = db.one(
        "SELECT id FROM resume_versions WHERE kind='variant' AND parent_id=? AND role_family=? AND job_id IS NULL "
        "AND COALESCE(target_country,'')=COALESCE(?, '') AND status != 'archived' AND user_id=? ORDER BY id DESC LIMIT 1",
        (base["id"], role_family, country, user_id))
    if existing:
        prev = get(db, existing["id"], user_id=user_id)
        if prev and prev["structured"] == result.structured and prev["layout"] == result.layout:
            return prev  # identical content: no new version
    label = ROLE_FAMILIES[role_family]["label"] + (f" · {settings_mod.COUNTRY_NAMES[country]}" if country else "")
    with db.transaction():
        rid = db.insert(
            """INSERT INTO resume_versions(label, kind, parent_id, role_family, target_country, structured_json,
                   changes_json, layout_json, style_json, method, status, created_at, user_id)
               VALUES (?, 'variant', ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (label, base["id"], role_family, country, dumps(result.structured), dumps(result.changes),
             dumps(result.layout), dumps(_style_record(result, fp)), result.method, iso(), user_id))
        audit(db, actor=actor, entity="resume", entity_id=rid, action="create_variant",
              detail={"role_family": role_family, "country": country, "changes": len(result.changes),
                      "company_type": result.style.get("company_type"), "pages": result.layout.get("pages")})
    return get(db, rid, user_id=user_id)


def create_job_resume(db: Database, job_id: int, actor: str = "system", ai=None, *, user_id: int) -> dict:
    """Resume tailored to one job (title, JD, employer -> company type, country conventions), fitted to one page
    and fact-guarded. Idempotent: same job + canonical resume + settings returns the existing version."""
    base = canonical(db, user_id=user_id)
    if not base:
        raise ResumeError("no_canonical", "No canonical resume imported yet.")
    job = db.one("SELECT j.*, e.name AS employer_name FROM jobs j JOIN employers e ON e.id=j.employer_id "
                 "WHERE j.id=?", (job_id,))
    if not job:
        raise LookupError("Job not found")
    s = settings_mod.get_all(db, user_id)
    prefs, cand = s["resume_style"], s["candidate"]
    country = job.get("country") if job.get("country") in settings_mod.COUNTRY_NAMES else None
    use_ai = _use_ai(ai, prefs, user_id)
    desc = job.get("description_text") or ""
    fp = fingerprint(base["id"], base.get("file_sha256"), job_id, job.get("title"), job.get("employer_name"),
                     country, hashlib.sha256(desc.encode()).hexdigest(), prefs, cand, use_ai)
    prev = None
    existing = db.one("SELECT id FROM resume_versions WHERE kind='variant' AND job_id=? AND parent_id=? "
                      "AND status != 'archived' AND user_id=? ORDER BY id DESC LIMIT 1", (job_id, base["id"], user_id))
    if existing:
        prev = get(db, existing["id"], user_id=user_id)
        if prev and prev["style"].get("fingerprint") == fp:
            return prev
    jd = jd_mod.analyze(job.get("title") or "", desc)
    jobd = {"title": job.get("title"), "country": country, "description_text": desc,
            "employer": job.get("employer_name")}
    result = tailor(base["structured"], base["analysis"], cand, prefs, job=jobd, jd=jd, country=country,
                    employer=job.get("employer_name"), ai=ai if use_ai else None)
    _guard(base, result, cand)
    if prev and prev["structured"] == result.structured and prev["layout"] == result.layout:
        return prev
    label = f"Tailored · {job.get('employer_name')} · {job.get('title')}"[:200]
    with db.transaction():
        rid = db.insert(
            """INSERT INTO resume_versions(label, kind, parent_id, role_family, target_country, job_id,
                   structured_json, changes_json, layout_json, style_json, method, status, created_at, user_id)
               VALUES (?, 'variant', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (label, base["id"], result.structured.get("role_family"), country, job_id, dumps(result.structured),
             dumps(result.changes), dumps(result.layout), dumps(_style_record(result, fp)), result.method, iso(),
             user_id))
        audit(db, actor=actor, entity="resume", entity_id=rid, action="create_job_resume",
              detail={"job_id": job_id, "company_type": result.style.get("company_type"),
                      "role_family": result.structured.get("role_family"), "pages": result.layout.get("pages"),
                      "method": result.method, "warnings": len(result.warnings)})
    return get(db, rid, user_id=user_id)


def job_resume(db: Database, job_id: int, *, user_id: int) -> dict | None:
    """Latest non-archived tailored resume for a job, if any."""
    row = db.one("SELECT id FROM resume_versions WHERE kind='variant' AND job_id=? AND status != 'archived' "
                 "AND user_id=? ORDER BY id DESC LIMIT 1", (job_id, user_id))
    return get(db, row["id"], user_id=user_id) if row else None


def export_version(v: dict, fmt: str) -> bytes:
    """PDF/DOCX bytes for a stored version using its persisted one-page layout (None for originals/legacy rows)."""
    if fmt == "pdf":
        return export_pdf(v["structured"], v.get("layout"))
    if fmt == "docx":
        return export_docx(v["structured"], v.get("layout"))
    raise ValueError("fmt must be 'pdf' or 'docx'")


def download_filename(v: dict, fmt: str) -> str:
    """e.g. 'HrithikRajendraDwivedi_CV_v12.pdf' ('CV' or 'Resume' per the target-country convention)."""
    st = v.get("structured") or {}
    name = "".join(ch for ch in (st.get("name") or "resume").title() if ch.isalnum()) or "Resume"
    return f"{name}_{st.get('doc_title') or 'Resume'}_v{v['id']}.{fmt}"


def update_variant_summary(db: Database, rid: int, summary: str, actor: str, *, user_id: int) -> dict:
    """User edit of a variant's summary, guarded against introducing unverified numbers/skills."""
    v = get(db, rid, user_id=user_id)
    if not v or v["kind"] != "variant":
        raise LookupError("Variant not found")
    base = get(db, v["parent_id"], user_id=user_id)
    if not base:
        raise LookupError("Variant not found")
    summary = " ".join(summary.split())[:2000]
    st = dict(v["structured"])
    st["summary"] = summary
    s = settings_mod.get_all(db, user_id)
    problems = verify_no_new_facts(base["structured"], st, s["candidate"],
                                   (v.get("style") or {}).get("guard_allowed_text", ""))
    if problems:
        raise ResumeError("fact_guard", "; ".join(problems) + ". Update the canonical resume first if this is true.")
    changes = v["changes"] + [{"section": "summary", "before": v["structured"].get("summary"), "after": summary,
                               "reason": "Edited by you"}]
    with db.transaction():
        db.run("UPDATE resume_versions SET structured_json=?, changes_json=?, status='draft' WHERE id=? AND user_id=?",
               (dumps(st), dumps(changes), rid, user_id))
        audit(db, actor=actor, entity="resume", entity_id=rid, action="edit_variant", detail={"field": "summary"})
    return get(db, rid, user_id=user_id)


def set_status(db: Database, rid: int, status: str, actor: str, *, user_id: int) -> None:
    if status not in ("draft", "approved", "archived"):
        raise ValueError("Invalid status")
    with db.transaction():
        n = db.run("UPDATE resume_versions SET status=? WHERE id=? AND kind='variant' AND user_id=?",
                   (status, rid, user_id))
        if n != 1:
            raise LookupError("Variant not found")
        audit(db, actor=actor, entity="resume", entity_id=rid, action=f"status_{status}")


def best_variant_for(db: Database, role_family: str, country: str | None, *, user_id: int) -> dict | None:
    row = db.one(
        "SELECT id FROM resume_versions WHERE kind='variant' AND role_family=? AND user_id=? "
        "AND COALESCE(target_country,'')=COALESCE(?, '') AND status IN ('draft','approved') "
        "ORDER BY CASE WHEN status='approved' THEN 0 ELSE 1 END, id DESC LIMIT 1", (role_family, user_id, country))
    return get(db, row["id"], user_id=user_id) if row else None
