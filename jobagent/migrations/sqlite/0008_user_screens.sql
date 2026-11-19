-- 0008: which screens each user may open. NULL = automatic (from the industries they target, see access.py);
-- otherwise a JSON list of the screens the administrator turned OFF for this user.
ALTER TABLE users ADD COLUMN screens TEXT;
