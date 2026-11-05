-- 0003: tag employers that belong to the user's "Top MNC" catalogue
ALTER TABLE employers ADD COLUMN mnc_key TEXT;
ALTER TABLE employers ADD COLUMN sector TEXT;
CREATE INDEX ix_employers_mnc ON employers(mnc_key);
