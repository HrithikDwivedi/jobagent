-- 0004: multi-user accounts with admin approval. Every private row gets an owner (user_id); jobs, employers,
-- listings, sponsorship evidence, the UK register and locks stay shared. Existing single-user data is
-- backfilled to the existing account, which becomes the administrator. Keep equivalent to postgres/0004.
-- Table rebuilds run with foreign_keys OFF (see Database.migrate) and are verified with foreign_key_check.

-- ---------------------------------------------------------------- accounts
ALTER TABLE users ADD COLUMN email TEXT;
ALTER TABLE users ADD COLUMN full_name TEXT;
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user'));
ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','suspended'));
ALTER TABLE users ADD COLUMN approved_at TEXT;
ALTER TABLE users ADD COLUMN approved_by INTEGER;
ALTER TABLE users ADD COLUMN created_ip TEXT;
-- the original single-user account (lowest id) becomes the administrator; any other pre-existing account stays usable
UPDATE users SET status = 'approved', approved_at = created_at;
UPDATE users SET role = 'admin' WHERE id = (SELECT MIN(id) FROM users);
CREATE UNIQUE INDEX ux_users_email ON users(email) WHERE email IS NOT NULL;
CREATE INDEX ix_users_status ON users(status);

-- ---------------------------------------------------------------- simple ownership columns
ALTER TABLE files ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE uploads ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE job_scores ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ai_usage ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE run_decisions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE resume_versions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;

UPDATE files SET user_id = (SELECT MIN(id) FROM users) WHERE kind <> 'uk_sponsor_register';
UPDATE uploads SET user_id = (SELECT MIN(id) FROM users);
UPDATE job_scores SET user_id = (SELECT MIN(id) FROM users);
UPDATE ai_usage SET user_id = (SELECT MIN(id) FROM users);
UPDATE resume_versions SET user_id = (SELECT MIN(id) FROM users);
UPDATE run_decisions SET user_id = (SELECT MIN(id) FROM users) WHERE decision NOT IN ('new','duplicate','updated','reopened','closed','invalid');

DROP INDEX ux_resume_one_canonical;
DROP INDEX ux_resume_original_sha;
CREATE UNIQUE INDEX ux_resume_one_canonical ON resume_versions(user_id) WHERE is_canonical = 1;
CREATE UNIQUE INDEX ux_resume_original_sha ON resume_versions(user_id, file_sha256) WHERE kind = 'original';
CREATE INDEX ix_resume_user ON resume_versions(user_id);
CREATE INDEX ix_files_user ON files(user_id);
CREATE INDEX ix_uploads_user ON uploads(user_id);
DROP INDEX ix_scores_job;
CREATE INDEX ix_scores_user_job ON job_scores(user_id, job_id, id);
CREATE INDEX ix_ai_usage_user ON ai_usage(user_id, at);
CREATE INDEX ix_runs_user ON runs(user_id);
CREATE INDEX ix_run_decisions_run ON run_decisions(run_id, user_id);

-- run_decisions: one decision per run/job/user (was per run/job)
CREATE TABLE run_decisions_new (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    job_id      INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    at          TEXT NOT NULL,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE
);
INSERT INTO run_decisions_new(id, run_id, job_id, decision, reason, at, user_id)
    SELECT id, run_id, job_id, decision, reason, at, user_id FROM run_decisions;
DROP TABLE run_decisions;
ALTER TABLE run_decisions_new RENAME TO run_decisions;
CREATE UNIQUE INDEX ux_run_decisions ON run_decisions(run_id, job_id, decision, COALESCE(user_id, 0));
CREATE INDEX ix_run_decisions_user ON run_decisions(run_id, user_id);

-- ---------------------------------------------------------------- applications: UNIQUE(job_id) -> UNIQUE(user_id, job_id)
CREATE TABLE applications_new (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE,
    job_id              INTEGER NOT NULL REFERENCES jobs(id),
    status              TEXT NOT NULL CHECK (status IN (
                          'shortlisted','awaiting_approval','ready_to_apply','application_started',
                          'submitted','submission_uncertain','submission_failed','interview','assessment',
                          'offer','rejected','withdrawn','closed')),
    resume_version_id   INTEGER REFERENCES resume_versions(id),
    cover_letter_id     INTEGER REFERENCES cover_letters(id),
    answers_json        TEXT NOT NULL DEFAULT '[]',
    missing_info_json   TEXT NOT NULL DEFAULT '[]',
    external            INTEGER NOT NULL DEFAULT 0,
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
    row_version         INTEGER NOT NULL DEFAULT 1,
    is_sample           INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    auto_apply          INTEGER NOT NULL DEFAULT 0,
    attempts            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, job_id)
);
INSERT INTO applications_new(id, user_id, job_id, status, resume_version_id, cover_letter_id, answers_json,
        missing_info_json, external, started_at, submitted_at, submission_evidence_json, reference_number,
        submitted_url, interview_at, follow_up_at, notes, rejection_reason, offer_details, row_version, is_sample,
        created_at, updated_at, auto_apply, attempts)
    SELECT id, (SELECT MIN(id) FROM users), job_id, status, resume_version_id, cover_letter_id, answers_json,
        missing_info_json, external, started_at, submitted_at, submission_evidence_json, reference_number,
        submitted_url, interview_at, follow_up_at, notes, rejection_reason, offer_details, row_version, is_sample,
        created_at, updated_at, auto_apply, attempts FROM applications;
DROP TABLE applications;
ALTER TABLE applications_new RENAME TO applications;
CREATE INDEX ix_apps_status ON applications(status);
CREATE INDEX ix_apps_user_status ON applications(user_id, status);
CREATE INDEX ix_apps_job ON applications(job_id);

-- ---------------------------------------------------------------- cover letters: version numbers per user and job
CREATE TABLE cover_letters_new (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_version_id INTEGER REFERENCES resume_versions(id),
    version         INTEGER NOT NULL,
    body            TEXT NOT NULL,
    method          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','archived')),
    created_at      TEXT NOT NULL,
    UNIQUE (user_id, job_id, version)
);
INSERT INTO cover_letters_new(id, user_id, job_id, resume_version_id, version, body, method, status, created_at)
    SELECT id, (SELECT MIN(id) FROM users), job_id, resume_version_id, version, body, method, status, created_at
    FROM cover_letters;
DROP TABLE cover_letters;
ALTER TABLE cover_letters_new RENAME TO cover_letters;
CREATE INDEX ix_cover_letters_user ON cover_letters(user_id, job_id);

-- ---------------------------------------------------------------- prep plans: PK (user_id, job_id)
CREATE TABLE prep_plans_new (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    plan_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, job_id)
);
INSERT INTO prep_plans_new(user_id, job_id, plan_json, created_at)
    SELECT (SELECT MIN(id) FROM users), job_id, plan_json, created_at FROM prep_plans
    WHERE EXISTS (SELECT 1 FROM users);
DROP TABLE prep_plans;
ALTER TABLE prep_plans_new RENAME TO prep_plans;

-- ---------------------------------------------------------------- skill progress: PK (user_id, skill)
CREATE TABLE skill_progress_new (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    skill       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started' CHECK (status IN ('not_started','learning','practising','confident')),
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, skill)
);
INSERT INTO skill_progress_new(user_id, skill, status, notes, updated_at)
    SELECT (SELECT MIN(id) FROM users), skill, status, notes, updated_at FROM skill_progress
    WHERE EXISTS (SELECT 1 FROM users);
DROP TABLE skill_progress;
ALTER TABLE skill_progress_new RENAME TO skill_progress;

-- ---------------------------------------------------------------- per-user settings (global keys stay in `settings`)
CREATE TABLE user_settings (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
INSERT INTO user_settings(user_id, key, value_json, updated_at)
    SELECT (SELECT MIN(id) FROM users), key, value_json, updated_at FROM settings
    WHERE key IN ('candidate','target_countries','include_india_remote','preferred_roles','excluded_title_terms',
                  'min_salary','currency','sponsorship_policy','work_arrangements','max_applications_per_run',
                  'max_applications_per_day','max_applications_per_week','min_match_score','weights','auto_apply',
                  'resume_style','approval_ttl_hours','verification_max_age_hours','uncertain_after_hours',
                  'notifications','data_retention_days')
      AND EXISTS (SELECT 1 FROM users);
DELETE FROM settings
    WHERE key IN ('candidate','target_countries','include_india_remote','preferred_roles','excluded_title_terms',
                  'min_salary','currency','sponsorship_policy','work_arrangements','max_applications_per_run',
                  'max_applications_per_day','max_applications_per_week','min_match_score','weights','auto_apply',
                  'resume_style','approval_ttl_hours','verification_max_age_hours','uncertain_after_hours',
                  'notifications','data_retention_days')
      AND EXISTS (SELECT 1 FROM users);

-- ---------------------------------------------------------------- bookmarks per user (jobs.bookmarked is no longer used)
CREATE TABLE user_bookmarks (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, job_id)
);
INSERT INTO user_bookmarks(user_id, job_id, created_at)
    SELECT (SELECT MIN(id) FROM users), id, last_seen_at FROM jobs WHERE bookmarked = 1 AND EXISTS (SELECT 1 FROM users);
UPDATE jobs SET bookmarked = 0 WHERE bookmarked = 1 AND EXISTS (SELECT 1 FROM users);

-- ---------------------------------------------------------------- signed sign-up review links (single use)
CREATE TABLE signup_tokens (
    nonce       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('approve','reject')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX ix_signup_tokens_user ON signup_tokens(user_id);

-- ---------------------------------------------------------------- outgoing email log
CREATE TABLE outbox (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    to_addr     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body_text   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('queued','sent','logged','failed')),
    provider    TEXT NOT NULL,
    error       TEXT,
    related_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE INDEX ix_outbox_created ON outbox(created_at);
