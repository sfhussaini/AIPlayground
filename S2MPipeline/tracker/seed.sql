INSERT OR IGNORE INTO user (account_id, username, display_name, email) VALUES
    ('u-admin', 'admin', 'Admin User', 'admin@example.com'),
    ('u-dev',   'dev',   'Dev User',   'dev@example.com'),
    ('u-qa',    'qa',    'QA User',    'qa@example.com');

INSERT OR IGNORE INTO issue_type (id, name, hierarchy_level, is_leaf) VALUES
    (1, 'Deliverable', 0, 0),
    (2, 'Epic',        1, 0),
    (3, 'Story',       2, 0),
    (4, 'Task',        2, 0),
    (5, 'Bug',         2, 1),
    (6, 'Sub-task',    3, 1);

INSERT OR IGNORE INTO status_category (id, name) VALUES
    (1, 'To Do'),
    (2, 'In Progress'),
    (3, 'Done');

INSERT OR IGNORE INTO status (id, name, category_id) VALUES
    (1,  'Draft',                1),
    (2,  'In Progress',          2),
    (3,  'In Review',            2),
    (4,  'Ready for Acceptance', 2),
    (5,  'Criteria Verified',    2),
    (6,  'Fixed',                2),
    (7,  'Verified',             2),
    (8,  'Done',                 3),
    (9,  'Accepted',             3),
    (10, 'Cancelled',            3);

INSERT OR IGNORE INTO priority (id, name, sequence) VALUES
    (1, 'Highest', 1), (2, 'High', 2), (3, 'Medium', 3), (4, 'Low', 4), (5, 'Lowest', 5);

INSERT OR IGNORE INTO resolution (id, name) VALUES
    (1, 'Fixed'), (2, 'Won''t Fix'), (3, 'Duplicate'), (4, 'Cannot Reproduce');

INSERT OR IGNORE INTO issue_link_type (id, name, inward_description, outward_description, is_blocking) VALUES
    (1, 'Blocks',     'is blocked by', 'blocks',       1),
    (2, 'Relates',    'relates to',    'relates to',   0),
    (3, 'Duplicates', 'is duplicated by', 'duplicates', 0),
    (4, 'Tests',      'is tested by',  'tests',        0),
    (5, 'Implements', 'is implemented by', 'implements', 0);