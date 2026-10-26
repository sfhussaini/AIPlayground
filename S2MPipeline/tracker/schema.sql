PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user (
    account_id      TEXT PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    display_name    TEXT NOT NULL,
    email           TEXT UNIQUE,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS project (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key     TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    lead_account_id TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS issue_type (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    hierarchy_level INTEGER NOT NULL,
    is_leaf         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS status_category (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS status (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    category_id     INTEGER NOT NULL REFERENCES status_category(id)
);

CREATE TABLE IF NOT EXISTS priority (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    sequence        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resolution (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS issue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key          TEXT UNIQUE NOT NULL,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE RESTRICT,
    issue_type_id      INTEGER NOT NULL REFERENCES issue_type(id),
    parent_id          INTEGER REFERENCES issue(id) ON DELETE RESTRICT,
    deliverable_root_id INTEGER REFERENCES issue(id) ON DELETE RESTRICT,
    summary            TEXT NOT NULL,
    description        TEXT,
    status_id          INTEGER NOT NULL REFERENCES status(id),
    priority_id        INTEGER REFERENCES priority(id),
    resolution_id      INTEGER REFERENCES resolution(id),
    reporter_id        TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    assignee_id        TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    resolved_at        TEXT,
    resolved_by        TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    verified_at        TEXT,
    verified_by        TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    custom_fields      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX IF NOT EXISTS ix_issue_project   ON issue(project_id, status_id);
CREATE INDEX IF NOT EXISTS ix_issue_root      ON issue(deliverable_root_id);
CREATE INDEX IF NOT EXISTS ix_issue_parent    ON issue(parent_id);
CREATE INDEX IF NOT EXISTS ix_issue_type      ON issue(issue_type_id, status_id);

CREATE TABLE IF NOT EXISTS deliverable (
    issue_id             INTEGER PRIMARY KEY REFERENCES issue(id) ON DELETE CASCADE,
    acceptance_criteria  TEXT NOT NULL,
    target_date          TEXT,
    criteria_verified_at TEXT,
    criteria_verified_by TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    accepted_at          TEXT,
    accepted_by          TEXT REFERENCES user(account_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS issue_link_type (
    id                  INTEGER PRIMARY KEY,
    name                TEXT UNIQUE NOT NULL,
    inward_description  TEXT NOT NULL,
    outward_description TEXT NOT NULL,
    is_blocking         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS issue_link (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    link_type_id          INTEGER NOT NULL REFERENCES issue_link_type(id),
    source_issue_id       INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    destination_issue_id  INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    created_by            TEXT REFERENCES user(account_id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (link_type_id, source_issue_id, destination_issue_id),
    CHECK (source_issue_id <> destination_issue_id)
);
CREATE INDEX IF NOT EXISTS ix_link_dest ON issue_link(destination_issue_id, link_type_id);
CREATE INDEX IF NOT EXISTS ix_link_src  ON issue_link(source_issue_id);

CREATE TABLE IF NOT EXISTS version (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    description  TEXT,
    release_date TEXT,
    is_released  INTEGER NOT NULL DEFAULT 0,
    released_at  TEXT,
    CHECK ((is_released = 0 AND released_at IS NULL)
        OR (is_released = 1 AND released_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS issue_version (
    issue_id     INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    version_id   INTEGER NOT NULL REFERENCES version(id) ON DELETE RESTRICT,
    relationship TEXT NOT NULL CHECK (relationship IN ('FIX_VERSION','AFFECTS_VERSION','DELIVERS')),
    PRIMARY KEY (issue_id, version_id, relationship)
);

CREATE TABLE IF NOT EXISTS comment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    author_id  TEXT REFERENCES user(account_id) ON DELETE SET NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS issue_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    author_id  TEXT REFERENCES user(account_id),
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_hist_issue ON issue_history(issue_id);