-- 0006: per-user AI access granted by the administrator (off for every new account), a help desk
-- (tickets, messages, administrator-curated answers) and personal access tokens for the Application Companion
-- browser extension. Keep equivalent to postgres/0006.
ALTER TABLE users ADD COLUMN ai_access INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN ai_monthly_limit_usd REAL;
ALTER TABLE users ADD COLUMN ai_granted_at TEXT;
ALTER TABLE users ADD COLUMN ai_granted_by INTEGER;
UPDATE users SET ai_access = 1, ai_granted_at = created_at WHERE role = 'admin';

CREATE TABLE support_tickets (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','resolved','closed')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_staff_at TEXT,
    last_user_at  TEXT,
    user_seen_at  TEXT
);
CREATE INDEX ix_support_tickets_user ON support_tickets(user_id, status);
CREATE INDEX ix_support_tickets_status ON support_tickets(status, updated_at);

CREATE TABLE support_messages (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    author_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_staff    INTEGER NOT NULL DEFAULT 0,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX ix_support_messages_ticket ON support_messages(ticket_id, id);

CREATE TABLE support_answers (
    id          INTEGER PRIMARY KEY,
    category    TEXT NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    keywords    TEXT NOT NULL DEFAULT '',
    helpful     INTEGER NOT NULL DEFAULT 0,
    source_ticket_id INTEGER REFERENCES support_tickets(id) ON DELETE SET NULL,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX ix_api_tokens_user ON api_tokens(user_id);
