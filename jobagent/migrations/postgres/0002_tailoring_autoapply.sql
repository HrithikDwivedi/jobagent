-- 0002: job-specific tailored resumes, layout metadata, and policy-based (auto-apply) approvals
ALTER TABLE resume_versions ADD COLUMN job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE resume_versions ADD COLUMN layout_json TEXT;
ALTER TABLE resume_versions ADD COLUMN style_json TEXT;
ALTER TABLE approvals ADD COLUMN approved_by TEXT NOT NULL DEFAULT 'user';
ALTER TABLE applications ADD COLUMN auto_apply BIGINT NOT NULL DEFAULT 0;
ALTER TABLE applications ADD COLUMN attempts BIGINT NOT NULL DEFAULT 0;
CREATE INDEX ix_resume_job ON resume_versions(job_id);
