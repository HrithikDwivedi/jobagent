-- 0001: initial schema

CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- resume_original | submission_evidence
    original_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    data        BLOB NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX ix_files_sha ON files(sha256);

CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,         -- stored lower-case
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE TABLE sessions (
    token_hash      TEXT PRIMARY KEY,           -- sha256 of the cookie value; raw token never stored
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL
);
CREATE INDEX ix_sessions_user ON sessions(user_id);

CREATE TABLE login_attempts (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL,
    ip          TEXT NOT NULL,
    success     INTEGER NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX ix_login_attempts_ip_at ON login_attempts(ip, at);
CREATE INDEX ix_login_attempts_user_at ON login_attempts(username, at);

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE employers (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    website         TEXT,
    uk_sponsor_status TEXT,          -- 'listed' | 'not_listed' | NULL (unchecked)
    uk_sponsor_routes TEXT,
    uk_sponsor_checked_at TEXT,
    relocation_policy TEXT,
    relocation_source TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY,
    job_key             TEXT NOT NULL UNIQUE,     -- stable identity (see normalize.job_identity)
    employer_id         INTEGER NOT NULL REFERENCES employers(id),
    title               TEXT NOT NULL,
    normalized_title    TEXT NOT NULL,
    requisition_id      TEXT,
    location_raw        TEXT,
    country             TEXT,                     -- ISO 3166-1 alpha-2 or NULL if unknown
    city                TEXT,
    remote_type         TEXT NOT NULL DEFAULT 'unknown', -- onsite|hybrid|remote|unknown
    remote_scope        TEXT,                     -- e.g. 'india_allowed', 'eu_only', 'worldwide'
    employment_type     TEXT,
    seniority           TEXT,
    experience_min_years REAL,
    experience_text     TEXT,
    description_text    TEXT NOT NULL DEFAULT '',
    description_hash    TEXT,
    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT,
    salary_period       TEXT,                     -- year|month|hour
    salary_source       TEXT,                     -- 'listing' | NULL
    languages_required  TEXT,                     -- JSON list of {language, level, required}
    education_text      TEXT,
    relocation_text     TEXT,
    work_auth_text      TEXT,
    deadline            TEXT,
    published_at        TEXT,
    first_discovered_at TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    last_verified_at    TEXT,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','expired','unverified')),
    apply_url           TEXT,
    canonical_url       TEXT,
    bookmarked          INTEGER NOT NULL DEFAULT 0,
    is_sample           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_jobs_employer ON jobs(employer_id);
CREATE INDEX ix_jobs_country ON jobs(country);
CREATE INDEX ix_jobs_status ON jobs(status);
CREATE INDEX ix_jobs_discovered ON jobs(first_discovered_at);
CREATE INDEX ix_jobs_emp_title ON jobs(employer_id, normalized_title);
CREATE UNIQUE INDEX ux_jobs_employer_req ON jobs(employer_id, requisition_id) WHERE requisition_id IS NOT NULL;

-- Every place a job was seen. Many listings can point at one job (cross-posting, reposts).
CREATE TABLE job_listings (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    source_job_id   TEXT NOT NULL,
    url             TEXT,
    canonical_url   TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);
CREATE INDEX ix_listings_job ON job_listings(job_id);
CREATE UNIQUE INDEX ux_listings_canonical_url ON job_listings(canonical_url) WHERE canonical_url IS NOT NULL;

CREATE TABLE sponsorship_assessments (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    classification  TEXT NOT NULL CHECK (classification IN ('confirmed','likely','unknown','not_supported')),
    evidence_json   TEXT NOT NULL,     -- list of {kind, text, source, checked_at}
    method          TEXT NOT NULL,     -- 'rules' | 'rules+ai'
    assessed_at     TEXT NOT NULL
);
CREATE INDEX ix_sponsor_job ON sponsorship_assessments(job_id, assessed_at);

CREATE TABLE runs (
    id              TEXT PRIMARY KEY,  -- uuid4
    kind            TEXT NOT NULL,
    trigger         TEXT NOT NULL DEFAULT 'manual',
    status          TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','partial','aborted')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    config_json     TEXT NOT NULL,
    sources_json    TEXT NOT NULL DEFAULT '[]',
    stats_json      TEXT NOT NULL DEFAULT '{}',
    errors_json     TEXT NOT NULL DEFAULT '[]',
    ai_calls        INTEGER NOT NULL DEFAULT 0,
    ai_tokens_in    INTEGER NOT NULL DEFAULT 0,
    ai_tokens_out   INTEGER NOT NULL DEFAULT 0,
    ai_cost_usd     REAL NOT NULL DEFAULT 0
);
CREATE INDEX ix_runs_started ON runs(started_at);

CREATE TABLE job_scores (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id          TEXT REFERENCES runs(id),
    score           REAL NOT NULL,
    recommendation  TEXT NOT NULL CHECK (recommendation IN ('apply','review','improve_skills','skip')),
    breakdown_json  TEXT NOT NULL,
    details_json    TEXT NOT NULL,
    weights_json    TEXT NOT NULL,
    profile_version TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_scores_job ON job_scores(job_id, created_at);

CREATE TABLE run_decisions (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    job_id      INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,     -- new|duplicate|updated|shortlisted|skipped|prepared|closed
    reason      TEXT NOT NULL,
    at          TEXT NOT NULL,
    UNIQUE (run_id, job_id, decision)
);

CREATE TABLE resume_versions (
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original','variant')),
    parent_id       INTEGER REFERENCES resume_versions(id),
    is_canonical    INTEGER NOT NULL DEFAULT 0,
    source_path     TEXT,
    source_file_id  INTEGER REFERENCES files(id),
    file_sha256     TEXT,
    role_family     TEXT,
    target_country  TEXT,
    structured_json TEXT NOT NULL,
    analysis_json   TEXT,
    changes_json    TEXT,          -- before/after list for variants
    method          TEXT NOT NULL DEFAULT 'rules',
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','archived')),
    pdf_path        TEXT,
    docx_path       TEXT,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_resume_one_canonical ON resume_versions(is_canonical) WHERE is_canonical = 1;
CREATE UNIQUE INDEX ux_resume_original_sha ON resume_versions(file_sha256) WHERE kind = 'original';

CREATE TABLE cover_letters (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_version_id INTEGER REFERENCES resume_versions(id),
    version         INTEGER NOT NULL,
    body            TEXT NOT NULL,
    method          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','archived')),
    created_at      TEXT NOT NULL,
    UNIQUE (job_id, version)
);

CREATE TABLE applications (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL UNIQUE REFERENCES jobs(id),  -- one application per job identity
    status              TEXT NOT NULL CHECK (status IN (
                          'shortlisted','awaiting_approval','ready_to_apply','application_started',
                          'submitted','submission_uncertain','submission_failed','interview','assessment',
                          'offer','rejected','withdrawn','closed')),
    resume_version_id   INTEGER REFERENCES resume_versions(id),
    cover_letter_id     INTEGER REFERENCES cover_letters(id),
    answers_json        TEXT NOT NULL DEFAULT '[]',
    missing_info_json   TEXT NOT NULL DEFAULT '[]',
    external            INTEGER NOT NULL DEFAULT 0,   -- applied outside JobAgent
    started_at          TEXT,
    submitted_at        TEXT,
    submission_evidence_json TEXT,
    reference_number    TEXT,
    submitted_url       TEXT,
    interview_at        TEXT,
    follow_up_at        TEXT,
    notes               TEXT NOT NULL DEFAULT '',
    rejection_reason    TEXT,
    offer_details       TEXT,
    row_version         INTEGER NOT NULL DEFAULT 1,   -- optimistic concurrency
    is_sample           INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX ix_apps_status ON applications(status);

CREATE TABLE approvals (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    fingerprint     TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,     -- what exactly was approved (employer, role, url, resume, answers)
    approved_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT,
    revoked_at      TEXT
);
CREATE INDEX ix_approvals_app ON approvals(application_id);
-- at most one unconsumed, unrevoked approval per application
CREATE UNIQUE INDEX ux_approvals_active ON approvals(application_id) WHERE consumed_at IS NULL AND revoked_at IS NULL;

-- Append-only, hash-chained audit trail
CREATE TABLE audit_events (
    id              INTEGER PRIMARY KEY,
    at              TEXT NOT NULL,
    actor           TEXT NOT NULL,     -- user:<name> | agent | system
    entity          TEXT NOT NULL,     -- application|job|resume|settings|auth|run
    entity_id       TEXT,
    action          TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT UNIQUE,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL
);
CREATE INDEX ix_audit_entity ON audit_events(entity, entity_id);
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TABLE locks (
    name        TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE ai_cache (
    cache_key   TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE ai_usage (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    cached      INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL,
    error       TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX ix_ai_usage_at ON ai_usage(at);

CREATE TABLE skill_progress (
    skill       TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'not_started' CHECK (status IN ('not_started','learning','practising','confident')),
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE prep_plans (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    plan_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE uploads (
    id          INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL
);
