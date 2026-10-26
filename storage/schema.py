SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS groups (
    group_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    enabled_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS group_config (
    group_id INTEGER PRIMARY KEY REFERENCES groups(group_id) ON DELETE CASCADE,
    base_probability REAL NOT NULL DEFAULT 20,
    cooldown_seconds INTEGER NOT NULL DEFAULT 300,
    must_answer INTEGER NOT NULL DEFAULT 1,
    games_enabled INTEGER NOT NULL DEFAULT 1,
    persona TEXT NOT NULL DEFAULT '标准贪吃鲸娘',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS group_state (
    group_id INTEGER PRIMARY KEY REFERENCES groups(group_id) ON DELETE CASCADE,
    hunger REAL NOT NULL DEFAULT 80,
    mood REAL NOT NULL DEFAULT 60,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS affection (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    daily_gain REAL NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS growth (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    exp INTEGER NOT NULL DEFAULT 0,
    daily_gain INTEGER NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS tokens (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0 CHECK(balance BETWEEN 0 AND 999),
    last_signin_day TEXT,
    last_chat_reward_at REAL,
    chat_reward_day TEXT,
    chat_reward_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS feed_cooldowns (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    feed_kind TEXT NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id, feed_kind)
);

CREATE TABLE IF NOT EXISTS bindings (
    user_id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    source_message_id INTEGER,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_nicknames (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER,
    nickname TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'bot')),
    content TEXT NOT NULL,
    search_text TEXT NOT NULL,
    onebot_message_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group_time
    ON messages(group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_onebot_id
    ON messages(onebot_message_id);

CREATE TABLE IF NOT EXISTS private_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'bot')),
    content TEXT NOT NULL,
    search_text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_private_group_user_time
    ON private_messages(group_id, user_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    search_text,
    tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS private_messages_fts USING fts5(
    content,
    search_text,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS group_memes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    source_message_id INTEGER,
    source_date TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memes_group_date
    ON group_memes(group_id, source_date);

CREATE TABLE IF NOT EXISTS user_profiles (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    preferences TEXT NOT NULL DEFAULT '',
    interaction_style TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    source_date TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS blocked_terms (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    normalized_term TEXT NOT NULL,
    original_term TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(group_id, normalized_term)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    actor TEXT,
    group_id INTEGER,
    user_key TEXT,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost_cny REAL NOT NULL,
    call_type TEXT NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_usage_time ON api_usage(created_at DESC);

CREATE TABLE IF NOT EXISTS job_runs (
    group_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    error_summary TEXT,
    PRIMARY KEY(group_id, run_date, job_type)
);

CREATE TABLE IF NOT EXISTS send_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    sent_at REAL,
    scene TEXT NOT NULL,
    group_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('reserved', 'sent', 'failed')),
    onebot_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_send_events_time
    ON send_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_send_events_group_sent
    ON send_events(group_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO system_settings(key, value) VALUES('global_paused', '0')
    ON CONFLICT(key) DO NOTHING;
INSERT INTO system_settings(key, value) VALUES('budget_alert_month', '')
    ON CONFLICT(key) DO NOTHING;
"""
