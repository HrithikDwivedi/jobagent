-- 0007: every industry, not only software. Jobs carry the industry and role they were classified into (taxonomy.py;
-- rows that existed before are classified lazily by taxonomy backfill), and accounts record when they finished the
-- onboarding questions (existing accounts count as onboarded so they are not interrupted). Keep equal to sqlite/0007.
ALTER TABLE users ADD COLUMN onboarded_at TEXT;
UPDATE users SET onboarded_at = created_at;
ALTER TABLE jobs ADD COLUMN industry TEXT;
ALTER TABLE jobs ADD COLUMN role_key TEXT;
ALTER TABLE jobs ADD COLUMN classified_at TEXT;
CREATE INDEX ix_jobs_industry ON jobs(industry, role_key);
