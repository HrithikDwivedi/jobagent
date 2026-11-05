-- 0004: multi-user accounts with admin approval (PostgreSQL). Equivalent to sqlite/0004_multiuser.sql; keep in sync.
-- Every private row gets an owner (user_id); jobs, employers, listings, sponsorship evidence, the UK register and
-- locks stay shared. Existing single-user data is backfilled to the existing account, which becomes the admin.

-- ---------------------------------------------------------------- accounts
ALTER TABLE users ADD COLUMN email TEXT;
ALTER TABLE users ADD COLUMN full_name TEXT;
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user'));
ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','suspended'));
ALTER TABLE users ADD COLUMN approved_at TEXT;
ALTER TABLE users ADD COLUMN approved_by BIGINT;
ALTER TABLE users ADD COLUMN created_ip TEXT;
UPDATE users SET status = 'approved', approved_at = created_at;
UPDATE users SET role = 'admin' WHERE id = (SELECT MIN(id) FROM users);
CREATE UNIQUE INDEX ux_users_email ON users(email) WHERE email IS NOT NULL;
CREATE INDEX ix_users_status ON users(status);

-- ---------------------------------------------------------------- simple ownership columns
ALTER TABLE files ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE uploads ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE job_scores ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ai_usage ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE run_decisions ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE resume_versions ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;

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

ALTER TABLE run_decisions DROP CONSTRAINT IF EXISTS run_decisions_run_id_job_id_decision_key;
CREATE UNIQUE INDEX ux_run_decisions ON run_decisions(run_id, job_id, decision, COALESCE(user_id, 0));
CREATE INDEX ix_run_decisions_user ON run_decisions(run_id, user_id);

-- ---------------------------------------------------------------- applications: UNIQUE(job_id) -> UNIQUE(user_id, job_id)
ALTER TABLE applications ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
UPDATE applications SET user_id = (SELECT MIN(id) FROM users);
ALTER TABLE applications DROP CONSTRAINT IF EXISTS applications_job_id_key;
ALTER TABLE applications ADD CONSTRAINT applications_user_job_key UNIQUE (user_id, job_id);
CREATE INDEX ix_apps_user_status ON applications(user_id, status);
CREATE INDEX ix_apps_job ON applications(job_id);

-- ---------------------------------------------------------------- cover letters: version numbers per user and job
ALTER TABLE cover_letters ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
UPDATE cover_letters SET user_id = (SELECT MIN(id) FROM users);
ALTER TABLE cover_letters DROP CONSTRAINT IF EXISTS cover_letters_job_id_version_key;
ALTER TABLE cover_letters ADD CONSTRAINT cover_letters_user_job_version_key UNIQUE (user_id, job_id, version);
CREATE INDEX ix_cover_letters_user ON cover_letters(user_id, job_id);

-- ---------------------------------------------------------------- prep plans: PK (user_id, job_id)
ALTER TABLE prep_plans ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
UPDATE prep_plans SET user_id = (SELECT MIN(id) FROM users);
DELETE FROM prep_plans WHERE user_id IS NULL;
ALTER TABLE prep_plans DROP CONSTRAINT IF EXISTS prep_plans_pkey;
ALTER TABLE prep_plans ALTER COLUMN job_id DROP IDENTITY IF EXISTS;
ALTER TABLE prep_plans ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE prep_plans ADD CONSTRAINT prep_plans_pkey PRIMARY KEY (user_id, job_id);

-- ---------------------------------------------------------------- skill progress: PK (user_id, skill)
ALTER TABLE skill_progress ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
UPDATE skill_progress SET user_id = (SELECT MIN(id) FROM users);
DELETE FROM skill_progress WHERE user_id IS NULL;
ALTER TABLE skill_progress DROP CONSTRAINT IF EXISTS skill_progress_pkey;
ALTER TABLE skill_progress ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE skill_progress ADD CONSTRAINT skill_progress_pkey PRIMARY KEY (user_id, skill);

-- ---------------------------------------------------------------- per-user settings (global keys stay in `settings`)
CREATE TABLE user_settings (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id      BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, job_id)
);
INSERT INTO user_bookmarks(user_id, job_id, created_at)
    SELECT (SELECT MIN(id) FROM users), id, last_seen_at FROM jobs WHERE bookmarked = 1 AND EXISTS (SELECT 1 FROM users);
UPDATE jobs SET bookmarked = 0 WHERE bookmarked = 1 AND EXISTS (SELECT 1 FROM users);

-- ---------------------------------------------------------------- signed sign-up review links (single use)
CREATE TABLE signup_tokens (
    nonce       TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('approve','reject')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX ix_signup_tokens_user ON signup_tokens(user_id);

-- ---------------------------------------------------------------- outgoing email log
CREATE TABLE outbox (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    kind        TEXT NOT NULL,
    to_addr     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body_text   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('queued','sent','logged','failed')),
    provider    TEXT NOT NULL,
    error       TEXT,
    related_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE INDEX ix_outbox_created ON outbox(created_at);
