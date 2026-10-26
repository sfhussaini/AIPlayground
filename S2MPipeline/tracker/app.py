import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, g, render_template, abort, current_app

BASE         = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(BASE, 'tracker.db')
DEFAULT_USER = 'u-admin'

app = Flask(__name__, static_folder='static', template_folder='templates')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute('PRAGMA foreign_keys = ON')
        with open(os.path.join(BASE, 'schema.sql')) as f:
            db.executescript(f.read())
        with open(os.path.join(BASE, 'seed.sql')) as f:
            db.executescript(f.read())


def now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def rows(rs):
    return [dict(r) for r in rs]


def get_type(db, type_id):
    return db.execute('SELECT * FROM issue_type WHERE id = ?', (type_id,)).fetchone()


def get_type_id(db, name):
    return db.execute('SELECT id FROM issue_type WHERE name = ?', (name,)).fetchone()['id']


def get_status(db, status_id):
    return db.execute('''
        SELECT s.*, c.name AS category_name
        FROM status s JOIN status_category c ON c.id = s.category_id
        WHERE s.id = ?
    ''', (status_id,)).fetchone()


def get_status_id(db, name):
    return db.execute('SELECT id FROM status WHERE name = ?', (name,)).fetchone()['id']


def get_priority_id(db, name):
    return db.execute('SELECT id FROM priority WHERE name = ?', (name,)).fetchone()['id']


def get_link_type_id(db, name):
    return db.execute('SELECT id FROM issue_link_type WHERE name = ?', (name,)).fetchone()['id']


def get_done_cat_id(db):
    return db.execute("SELECT id FROM status_category WHERE name = 'Done'").fetchone()['id']


def next_issue_key(db, project_id):
    p = db.execute('SELECT project_key FROM project WHERE id = ?', (project_id,)).fetchone()
    prefix = p['project_key']
    last = db.execute(
        'SELECT issue_key FROM issue WHERE project_id = ? AND issue_key LIKE ? '
        'ORDER BY id DESC LIMIT 1',
        (project_id, f'{prefix}-%')
    ).fetchone()
    n = 1
    if last:
        try:
            n = int(last['issue_key'].rsplit('-', 1)[1]) + 1
        except (ValueError, IndexError):
            pass
    return f'{prefix}-{n}'


def get_issue(db, issue_id):
    r = db.execute('SELECT * FROM issue WHERE id = ?', (issue_id,)).fetchone()
    if not r:
        abort(404, 'Issue not found')
    return r


def log_history(db, issue_id, field, old, new, author=DEFAULT_USER):
    db.execute(
        'INSERT INTO issue_history (issue_id, author_id, field, old_value, new_value, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (issue_id, author, field,
         None if old is None else str(old),
         None if new is None else str(new),
         now()))


def validate_hierarchy(db, type_id, parent_id, project_id):
    t = get_type(db, type_id)
    if t is None:
        raise ValueError('Unknown issue type')
    if t['name'] in ('Bug', 'Deliverable') and parent_id is not None:
        raise ValueError(f'{t["name"]} cannot have a parent')
    if parent_id is None:
        return
    parent = db.execute('SELECT * FROM issue WHERE id = ?', (parent_id,)).fetchone()
    if not parent:
        raise ValueError('Parent not found')
    pt = get_type(db, parent['issue_type_id'])
    if pt['is_leaf']:
        raise ValueError(f'{pt["name"]} cannot have children')
    if pt['hierarchy_level'] >= t['hierarchy_level']:
        raise ValueError(f'Invalid hierarchy: {pt["name"]} cannot parent {t["name"]}')
    if t['name'] == 'Sub-task' and pt['name'] not in ('Story', 'Task'):
        raise ValueError('Sub-task parent must be Story or Task')
    if parent['project_id'] != project_id:
        raise ValueError('Parent is in a different project')


def blocking_bugs_for_issue(db, issue_id):
    done = get_done_cat_id(db)
    rs = db.execute('''
        SELECT DISTINCT b.id
        FROM issue_link l
        JOIN issue_link_type lt ON lt.id = l.link_type_id AND lt.is_blocking = 1
        JOIN issue b            ON b.id = l.source_issue_id
        JOIN issue_type bt      ON bt.id = b.issue_type_id AND bt.name = 'Bug'
        JOIN status s           ON s.id = b.status_id
        WHERE l.destination_issue_id = ? AND s.category_id <> ?
    ''', (issue_id, done)).fetchall()
    return [r['id'] for r in rs]


def blocking_bugs_in_subtree(db, deliverable_id):
    done = get_done_cat_id(db)
    rs = db.execute('''
        SELECT DISTINCT b.id
        FROM issue s
        JOIN issue_link l       ON l.destination_issue_id = s.id
        JOIN issue_link_type lt ON lt.id = l.link_type_id AND lt.is_blocking = 1
        JOIN issue b            ON b.id = l.source_issue_id
        JOIN issue_type bt      ON bt.id = b.issue_type_id AND bt.name = 'Bug'
        JOIN status st          ON st.id = b.status_id
        WHERE s.deliverable_root_id = ? AND st.category_id <> ?
    ''', (deliverable_id, done)).fetchall()
    return [r['id'] for r in rs]


def open_subtasks(db, issue_id):
    done = get_done_cat_id(db)
    return [r['id'] for r in db.execute('''
        SELECT i.id FROM issue i JOIN status s ON s.id = i.status_id
        WHERE i.parent_id = ? AND s.category_id <> ?
    ''', (issue_id, done)).fetchall()]


def ticket_stats(db, deliverable_id):
    done = get_done_cat_id(db)
    r = db.execute('''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN s.category_id = ? THEN 1 ELSE 0 END) AS done
        FROM issue i
        JOIN issue_type t ON t.id = i.issue_type_id AND t.name IN ('Story','Task')
        JOIN status s     ON s.id = i.status_id
        WHERE i.deliverable_root_id = ?
    ''', (done, deliverable_id)).fetchone()
    return r['total'] or 0, r['done'] or 0


def compute_readiness(db, deliverable_id):
    total, done = ticket_stats(db, deliverable_id)
    bugs = blocking_bugs_in_subtree(db, deliverable_id)
    d = db.execute('SELECT * FROM deliverable WHERE issue_id = ?', (deliverable_id,)).fetchone()
    crit_ok  = bool(d and d['acceptance_criteria'])
    verified = bool(d and d['criteria_verified_at'])
    return {
        'tickets_total': total,
        'tickets_done': done,
        'open_blocking_bugs': len(bugs),
        'blocking_bug_ids': bugs,
        'criteria_present': crit_ok,
        'criteria_verified': verified,
        'ready_for_verification': (total > 0 and total == done and not bugs and crit_ok),
        'ready_for_acceptance': (verified and not bugs),
    }


TRANSITIONS = {
    'Deliverable': {
        'start':  {'from': ['Draft'], 'to': 'In Progress'},
        'ready':  {'from': ['In Progress'], 'to': 'Ready for Acceptance'},
        'verify': {'from': ['Ready for Acceptance'], 'to': 'Criteria Verified'},
        'return': {'from': ['Criteria Verified'], 'to': 'In Progress'},
        'accept': {'from': ['Criteria Verified'], 'to': 'Accepted'},
        'cancel': {'from': ['Draft','In Progress','Ready for Acceptance','Criteria Verified'],
                   'to': 'Cancelled'},
    },
    'Ticket': {
        'start':  {'from': ['Draft'], 'to': 'In Progress'},
        'review': {'from': ['In Progress'], 'to': 'In Review'},
        'return': {'from': ['In Review'], 'to': 'In Progress'},
        'done':   {'from': ['In Progress','In Review'], 'to': 'Done'},
        'cancel': {'from': ['Draft','In Progress','In Review'], 'to': 'Cancelled'},
    },
    'Bug': {
        'start':   {'from': ['Draft'], 'to': 'In Progress'},
        'fix':     {'from': ['In Progress'], 'to': 'Fixed'},
        'reopen':  {'from': ['Fixed'], 'to': 'In Progress'},
        'verify':  {'from': ['Fixed'], 'to': 'Verified'},
        'close':   {'from': ['Verified'], 'to': 'Done'},
        'wontfix': {'from': ['Draft','In Progress','Fixed'], 'to': 'Cancelled'},
    },
}


def normalize_type_name(name):
    return 'Ticket' if name in ('Story', 'Task', 'Sub-task') else name


def apply_transition(db, issue, action, payload):
    t = get_type(db, issue['issue_type_id'])
    tname = normalize_type_name(t['name'])
    if tname not in TRANSITIONS:
        raise ValueError(f'No workflow for {t["name"]}')
    rule = TRANSITIONS[tname].get(action)
    if not rule:
        raise ValueError(f'Unknown action: {action}')
    current = get_status(db, issue['status_id'])
    if current['name'] not in rule['from']:
        raise ValueError(f'Cannot "{action}" from status "{current["name"]}"')

    if tname == 'Deliverable':
        d = db.execute('SELECT * FROM deliverable WHERE issue_id = ?', (issue['id'],)).fetchone()
        if action == 'start' and (not d or not d['acceptance_criteria']):
            raise ValueError('Acceptance criteria required to start')
        if action == 'ready':
            r = compute_readiness(db, issue['id'])
            if r['tickets_total'] == 0:
                raise ValueError('Deliverable must have at least one ticket')
            if not r['ready_for_verification']:
                raise ValueError(
                    f"Cannot mark ready: {r['open_blocking_bugs']} blocking bug(s), "
                    f"{r['tickets_total'] - r['tickets_done']} open ticket(s)")
        if action == 'verify':
            r = compute_readiness(db, issue['id'])
            if not r['ready_for_verification']:
                raise ValueError('Readiness conditions not met')
        if action == 'accept':
            r = compute_readiness(db, issue['id'])
            if not r['ready_for_acceptance']:
                raise ValueError('Criteria must be verified and all blocking bugs closed')

    if tname == 'Ticket' and action == 'done':
        bugs = blocking_bugs_for_issue(db, issue['id'])
        if bugs:
            raise ValueError(f'{len(bugs)} blocking bug(s) must be closed first')
        subs = open_subtasks(db, issue['id'])
        if subs:
            raise ValueError(f'{len(subs)} open sub-task(s)')
        if not payload.get('resolution_id'):
            raise ValueError('Resolution required')

    if tname == 'Bug' and action in ('fix', 'close', 'wontfix'):
        if not payload.get('resolution_id'):
            raise ValueError('Resolution required')

    to_status = db.execute('SELECT id, name FROM status WHERE name = ?', (rule['to'],)).fetchone()
    updates = {'status_id': to_status['id'], 'updated_at': now()}

    if tname == 'Deliverable':
        if action == 'verify':
            db.execute('UPDATE deliverable SET criteria_verified_at = ?, criteria_verified_by = ? '
                       'WHERE issue_id = ?', (now(), DEFAULT_USER, issue['id']))
        elif action == 'return':
            db.execute('UPDATE deliverable SET criteria_verified_at = NULL, criteria_verified_by = NULL '
                       'WHERE issue_id = ?', (issue['id'],))
        elif action == 'accept':
            db.execute('UPDATE deliverable SET accepted_at = ?, accepted_by = ? WHERE issue_id = ?',
                       (now(), DEFAULT_USER, issue['id']))

    if tname == 'Bug':
        if action == 'fix':
            updates['resolved_at'] = now()
            updates['resolved_by'] = DEFAULT_USER
            updates['resolution_id'] = payload['resolution_id']
        elif action == 'verify':
            updates['verified_at'] = now()
            updates['verified_by'] = DEFAULT_USER
        elif action == 'reopen':
            updates.update(resolved_at=None, resolved_by=None,
                           verified_at=None, verified_by=None, resolution_id=None)
        elif action in ('close', 'wontfix') and payload.get('resolution_id'):
            updates['resolution_id'] = payload['resolution_id']

    if tname == 'Ticket' and action == 'done':
        updates['resolution_id'] = payload['resolution_id']

    sets = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE issue SET {sets} WHERE id = ?', list(updates.values()) + [issue['id']])
    log_history(db, issue['id'], 'status', current['name'], to_status['name'])
    db.commit()
    return to_status['name']


# ================================================================
#  Routes
# ================================================================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/health')
def api_health():
    """Self-check: list every registered /api route. Visit /api/health
    to verify the running app has the correct version installed."""
    return jsonify({
        'ok': True,
        'routes': sorted(str(r.rule) for r in current_app.url_map.iter_rules()
                         if str(r.rule).startswith('/api')),
    })


@app.route('/api/meta')
def api_meta():
    db = get_db()
    return jsonify({
        'issue_types': rows(db.execute('SELECT * FROM issue_type ORDER BY hierarchy_level, id')),
        'statuses':    rows(db.execute('''SELECT s.*, c.name AS category_name
                                          FROM status s
                                          JOIN status_category c ON c.id = s.category_id
                                          ORDER BY s.id''')),
        'priorities':  rows(db.execute('SELECT * FROM priority ORDER BY sequence')),
        'resolutions': rows(db.execute('SELECT * FROM resolution ORDER BY id')),
        'link_types':  rows(db.execute('SELECT * FROM issue_link_type ORDER BY id')),
        'users':       rows(db.execute('SELECT * FROM user WHERE is_active = 1 ORDER BY username')),
    })


@app.route('/api/projects', methods=['GET'])
def list_projects():
    db = get_db()
    return jsonify(rows(db.execute('SELECT * FROM project ORDER BY name')))


@app.route('/api/projects', methods=['POST'])
def create_project():
    db = get_db()
    data = request.get_json() or {}
    key  = (data.get('project_key') or '').strip().upper()
    name = (data.get('name') or '').strip()
    if not key or not name:
        return jsonify({'error': 'project_key and name required'}), 400
    try:
        cur = db.execute(
            'INSERT INTO project (project_key, name, description, lead_account_id) VALUES (?, ?, ?, ?)',
            (key, name, data.get('description', ''), DEFAULT_USER))
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({'error': f'Cannot create project: {e}'}), 400
    return jsonify({'id': cur.lastrowid, 'project_key': key})


@app.route('/api/dashboard')
def api_dashboard():
    db       = get_db()
    done_cat = get_done_cat_id(db)

    def one(sql, params=()):
        return db.execute(sql, params).fetchone()['c']

    projects = rows(db.execute('SELECT id, project_key, name, description FROM project ORDER BY name'))
    health = []
    for p in projects:
        pid = p['id']
        d_total = one("""SELECT COUNT(*) c FROM issue i
                         JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Deliverable'
                         WHERE i.project_id = ?""", (pid,))
        d_open = one("""SELECT COUNT(*) c FROM issue i
                        JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Deliverable'
                        JOIN status s ON s.id = i.status_id
                        WHERE i.project_id = ? AND s.category_id <> ?""", (pid, done_cat))
        tk_total = one("""SELECT COUNT(*) c FROM issue i
                          JOIN issue_type t ON t.id = i.issue_type_id AND t.name IN ('Story','Task')
                          WHERE i.project_id = ?""", (pid,))
        tk_done = one("""SELECT COUNT(*) c FROM issue i
                         JOIN issue_type t ON t.id = i.issue_type_id AND t.name IN ('Story','Task')
                         JOIN status s ON s.id = i.status_id
                         WHERE i.project_id = ? AND s.category_id = ?""", (pid, done_cat))
        bg_total = one("""SELECT COUNT(*) c FROM issue i
                          JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Bug'
                          WHERE i.project_id = ?""", (pid,))
        bg_open = one("""SELECT COUNT(*) c FROM issue i
                         JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Bug'
                         JOIN status s ON s.id = i.status_id
                         WHERE i.project_id = ? AND s.category_id <> ?""", (pid, done_cat))
        bg_blocking = one("""SELECT COUNT(DISTINCT i.id) c FROM issue i
                             JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Bug'
                             JOIN status s ON s.id = i.status_id
                             JOIN issue_link l ON l.source_issue_id = i.id
                             JOIN issue_link_type lt ON lt.id = l.link_type_id AND lt.is_blocking = 1
                             WHERE i.project_id = ? AND s.category_id <> ?""", (pid, done_cat))
        health.append({**p,
            'deliverables_total': d_total, 'deliverables_open': d_open,
            'tickets_total': tk_total, 'tickets_done': tk_done,
            'bugs_total': bg_total, 'bugs_open': bg_open, 'bugs_blocking': bg_blocking,
            'progress_pct': round(100 * tk_done / tk_total) if tk_total else 0,
        })

    recent = rows(db.execute('''
        SELECT h.id, h.field, h.old_value, h.new_value, h.created_at,
               i.id AS issue_id, i.issue_key, i.summary,
               t.name AS type_name, p.name AS project_name,
               u.display_name AS author_name
        FROM issue_history h
        JOIN issue i        ON i.id = h.issue_id
        JOIN issue_type t   ON t.id = i.issue_type_id
        JOIN project p      ON p.id = i.project_id
        LEFT JOIN user u    ON u.account_id = h.author_id
        ORDER BY h.id DESC LIMIT 25
    '''))

    blockers = rows(db.execute('''
        SELECT DISTINCT b.id, b.issue_key, b.summary, p.name AS project_name,
               s.name AS status_name, c.name AS category_name,
               (SELECT GROUP_CONCAT(i2.issue_key, ', ')
                FROM issue_link l2
                JOIN issue i2 ON i2.id = l2.destination_issue_id
                WHERE l2.source_issue_id = b.id) AS blocks_keys
        FROM issue b
        JOIN issue_type t  ON t.id = b.issue_type_id AND t.name = 'Bug'
        JOIN status s      ON s.id = b.status_id
        JOIN status_category c ON c.id = s.category_id
        JOIN project p     ON p.id = b.project_id
        JOIN issue_link l  ON l.source_issue_id = b.id
        JOIN issue_link_type lt ON lt.id = l.link_type_id AND lt.is_blocking = 1
        WHERE c.id <> ?
        ORDER BY b.id DESC LIMIT 25
    ''', (done_cat,)))

    totals = {k: sum(h[k] for h in health) for k in (
        'deliverables_total', 'deliverables_open',
        'tickets_total', 'tickets_done',
        'bugs_total', 'bugs_open', 'bugs_blocking')}
    totals['projects'] = len(projects)

    return jsonify({
        'totals': totals,
        'project_health': health,
        'recent_activity': recent,
        'blockers': blockers,
    })


@app.route('/api/issues')
def api_list_issues():
    db = get_db()
    project_id = request.args.get('project_id', type=int)
    type_name  = request.args.get('type') or ''
    category   = request.args.get('category') or ''
    search     = (request.args.get('q') or '').strip()
    limit      = request.args.get('limit', default=300, type=int)

    sql = '''
        SELECT i.id, i.issue_key, i.summary, i.updated_at,
               i.priority_id, i.assignee_id,
               t.name AS type_name, t.hierarchy_level,
               s.name AS status_name, c.name AS category_name,
               pr.name AS priority_name, pr.sequence AS priority_seq,
               res.name AS resolution_name,
               p.project_key, p.name AS project_name, p.id AS project_id
        FROM issue i
        JOIN issue_type t       ON t.id = i.issue_type_id
        JOIN status s           ON s.id = i.status_id
        JOIN status_category c  ON c.id = s.category_id
        LEFT JOIN priority pr   ON pr.id = i.priority_id
        LEFT JOIN resolution res ON res.id = i.resolution_id
        JOIN project p          ON p.id = i.project_id
        WHERE 1=1
    '''
    params = []
    if project_id: sql += ' AND i.project_id = ?'; params.append(project_id)
    if type_name:  sql += ' AND t.name = ?'; params.append(type_name)
    if category:   sql += ' AND c.name = ?'; params.append(category)
    if search:
        sql += ' AND (i.summary LIKE ? OR i.issue_key LIKE ?)'
        params += [f'%{search}%', f'%{search}%']
    sql += ' ORDER BY i.id DESC LIMIT ?'; params.append(limit)

    return jsonify(rows(db.execute(sql, params)))


@app.route('/api/issues/<int:iid>')
def issue_detail(iid):
    db = get_db()
    issue = get_issue(db, iid)
    t = get_type(db, issue['issue_type_id'])
    detail = dict(issue)
    detail['type']   = dict(t)
    detail['status'] = dict(get_status(db, issue['status_id']))

    proj = db.execute('SELECT * FROM project WHERE id = ?', (issue['project_id'],)).fetchone()
    detail['project'] = dict(proj) if proj else {'id': issue['project_id'], 'name': '—', 'project_key': '—'}

    if issue['priority_id']:
        pr = db.execute('SELECT * FROM priority WHERE id = ?', (issue['priority_id'],)).fetchone()
        detail['priority'] = dict(pr) if pr else None
    if issue['resolution_id']:
        r = db.execute('SELECT * FROM resolution WHERE id = ?', (issue['resolution_id'],)).fetchone()
        detail['resolution'] = dict(r) if r else None
    if issue['reporter_id']:
        u = db.execute('SELECT * FROM user WHERE account_id = ?', (issue['reporter_id'],)).fetchone()
        detail['reporter'] = dict(u) if u else None
    if issue['assignee_id']:
        u = db.execute('SELECT * FROM user WHERE account_id = ?', (issue['assignee_id'],)).fetchone()
        detail['assignee'] = dict(u) if u else None

    if issue['parent_id']:
        parent = db.execute('SELECT * FROM issue WHERE id = ?', (issue['parent_id'],)).fetchone()
        if parent:
            detail['parent'] = {'id': parent['id'],
                                'issue_key': parent['issue_key'],
                                'summary': parent['summary']}
    if issue['deliverable_root_id'] and issue['deliverable_root_id'] != issue['id']:
        root = db.execute('SELECT * FROM issue WHERE id = ?', (issue['deliverable_root_id'],)).fetchone()
        if root:
            detail['deliverable_root'] = {'id': root['id'],
                                          'issue_key': root['issue_key'],
                                          'summary': root['summary']}

    detail['children'] = [
        {'id': c['id'], 'issue_key': c['issue_key'], 'summary': c['summary'],
         'status_id': c['status_id'],
         'status_name': get_status(db, c['status_id'])['name']}
        for c in db.execute('SELECT * FROM issue WHERE parent_id = ? ORDER BY id', (iid,))
    ]

    if t['name'] == 'Deliverable':
        d = db.execute('SELECT * FROM deliverable WHERE issue_id = ?', (iid,)).fetchone()
        detail['deliverable'] = dict(d) if d else {}
        detail['readiness']   = compute_readiness(db, iid)

    detail['links'] = rows(db.execute('''
        SELECT l.id, l.link_type_id, l.source_issue_id, l.destination_issue_id,
               lt.name AS link_name, lt.inward_description, lt.outward_description,
               lt.is_blocking,
               src.issue_key AS src_key, src.summary AS src_summary,
               dst.issue_key AS dst_key, dst.summary AS dst_summary
        FROM issue_link l
        JOIN issue_link_type lt ON lt.id = l.link_type_id
        JOIN issue src ON src.id = l.source_issue_id
        JOIN issue dst ON dst.id = l.destination_issue_id
        WHERE l.source_issue_id = ? OR l.destination_issue_id = ?
        ORDER BY l.id
    ''', (iid, iid)))

    if t['name'] in ('Story', 'Task', 'Deliverable'):
        detail['blocking_bug_ids'] = blocking_bugs_for_issue(db, iid)

    detail['comments'] = rows(db.execute('''
        SELECT c.*, u.display_name AS author_name
        FROM comment c
        LEFT JOIN user u ON u.account_id = c.author_id
        WHERE c.issue_id = ? ORDER BY c.id
    ''', (iid,)))

    detail['history'] = rows(db.execute('''
        SELECT h.*, u.display_name AS author_name
        FROM issue_history h
        LEFT JOIN user u ON u.account_id = h.author_id
        WHERE h.issue_id = ? ORDER BY h.id DESC LIMIT 100
    ''', (iid,)))

    return jsonify(detail)


@app.route('/api/projects/<int:pid>/board')
def project_board(pid):
    db = get_db()
    project = db.execute('SELECT * FROM project WHERE id = ?', (pid,)).fetchone()
    if not project:
        abort(404, 'Project not found')

    deliverables = db.execute('''
        SELECT i.* FROM issue i
        JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Deliverable'
        WHERE i.project_id = ? AND i.parent_id IS NULL ORDER BY i.id
    ''', (pid,)).fetchall()

    out = []
    for d in deliverables:
        d_dict = dict(d)
        d_ext  = db.execute('SELECT * FROM deliverable WHERE issue_id = ?', (d['id'],)).fetchone()
        d_dict['deliverable'] = dict(d_ext) if d_ext else {}
        d_dict['readiness']   = compute_readiness(db, d['id'])
        d_dict['status']      = dict(get_status(db, d['status_id']))
        if d['priority_id']:
            pr = db.execute('SELECT * FROM priority WHERE id = ?', (d['priority_id'],)).fetchone()
            d_dict['priority'] = dict(pr) if pr else None
        else:
            d_dict['priority'] = None

        tickets = db.execute('''
            SELECT i.* FROM issue i
            JOIN issue_type t ON t.id = i.issue_type_id AND t.name IN ('Story','Task')
            WHERE i.deliverable_root_id = ? AND i.issue_type_id <> 5 ORDER BY i.id
        ''', (d['id'],)).fetchall()

        ticket_list = []
        for tk in tickets:
            subs = db.execute('SELECT * FROM issue WHERE parent_id = ? ORDER BY id',
                              (tk['id'],)).fetchall()
            blocking = blocking_bugs_for_issue(db, tk['id'])
            pr = db.execute('SELECT * FROM priority WHERE id = ?', (tk['priority_id'],)).fetchone() if tk['priority_id'] else None
            ticket_list.append({
                'issue': dict(tk),
                'status': dict(get_status(db, tk['status_id'])),
                'priority': dict(pr) if pr else None,
                'subtasks': [dict(s) for s in subs],
                'blocking_bug_ids': blocking,
            })

        bugs = db.execute('''
            SELECT i.* FROM issue i
            JOIN issue_type t ON t.id = i.issue_type_id AND t.name = 'Bug'
            WHERE i.deliverable_root_id = ? ORDER BY i.id
        ''', (d['id'],)).fetchall()
        bug_list = []
        for b in bugs:
            links = db.execute('''
                SELECT l.destination_issue_id, i.issue_key, i.summary
                FROM issue_link l
                JOIN issue_link_type lt ON lt.id = l.link_type_id AND lt.name = 'Blocks'
                JOIN issue i ON i.id = l.destination_issue_id
                WHERE l.source_issue_id = ?
            ''', (b['id'],)).fetchall()
            bug_list.append({
                'issue': dict(b),
                'status': dict(get_status(db, b['status_id'])),
                'blocks': [dict(l) for l in links],
            })

        d_dict['tickets'] = ticket_list
        d_dict['bugs']    = bug_list
        out.append(d_dict)

    return jsonify({'project': dict(project), 'deliverables': out})


@app.route('/api/deliverables', methods=['POST'])
def create_deliverable():
    db = get_db()
    data = request.get_json() or {}
    try:
        pid = int(data['project_id'])
        summary  = (data['summary'] or '').strip()
        criteria = (data.get('acceptance_criteria') or '').strip()
        if not summary or not criteria:
            raise ValueError('summary and acceptance_criteria required')
        key = next_issue_key(db, pid)
        cur = db.execute('''
            INSERT INTO issue (issue_key, project_id, issue_type_id, summary, description,
                               status_id, priority_id, reporter_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (key, pid, get_type_id(db, 'Deliverable'), summary,
              data.get('description', ''), get_status_id(db, 'Draft'),
              get_priority_id(db, 'Medium'), DEFAULT_USER))
        issue_id = cur.lastrowid
        db.execute('UPDATE issue SET deliverable_root_id = ? WHERE id = ?', (issue_id, issue_id))
        db.execute('INSERT INTO deliverable (issue_id, acceptance_criteria, target_date) '
                   'VALUES (?, ?, ?)', (issue_id, criteria, data.get('target_date')))
        db.commit()
        return jsonify({'id': issue_id, 'issue_key': key})
    except (KeyError, ValueError, sqlite3.IntegrityError) as e:
        db.rollback()
        return jsonify({'error': str(e)}), 400


@app.route('/api/tickets', methods=['POST'])
def create_ticket():
    db = get_db()
    data = request.get_json() or {}
    try:
        pid       = int(data['project_id'])
        parent_id = int(data['parent_id'])
        type_name = data.get('issue_type', 'Story')
        type_id   = get_type_id(db, type_name)
        validate_hierarchy(db, type_id, parent_id, pid)
        parent = db.execute('SELECT * FROM issue WHERE id = ?', (parent_id,)).fetchone()
        root_id = parent['deliverable_root_id']
        if not root_id:
            raise ValueError('Parent has no deliverable root')
        key = next_issue_key(db, pid)
        cur = db.execute('''
            INSERT INTO issue (issue_key, project_id, issue_type_id, parent_id, deliverable_root_id,
                               summary, description, status_id, priority_id, reporter_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (key, pid, type_id, parent_id, root_id,
              (data['summary'] or '').strip(), data.get('description', ''),
              get_status_id(db, 'Draft'), get_priority_id(db, data.get('priority', 'Medium')),
              DEFAULT_USER))
        db.commit()
        return jsonify({'id': cur.lastrowid, 'issue_key': key})
    except (KeyError, ValueError, sqlite3.IntegrityError) as e:
        db.rollback()
        return jsonify({'error': str(e)}), 400


@app.route('/api/bugs', methods=['POST'])
def create_bug():
    db = get_db()
    data = request.get_json() or {}
    try:
        source_id = int(data['source_ticket_id'])
        source    = get_issue(db, source_id)
        root_id   = source['deliverable_root_id']
        if not root_id:
            raise ValueError('Source ticket has no deliverable root')
        key = next_issue_key(db, source['project_id'])
        cur = db.execute('''
            INSERT INTO issue (issue_key, project_id, issue_type_id, parent_id, deliverable_root_id,
                               summary, description, status_id, priority_id, reporter_id)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        ''', (key, source['project_id'], get_type_id(db, 'Bug'), root_id,
              (data['summary'] or '').strip(), data.get('description', ''),
              get_status_id(db, 'Draft'), get_priority_id(db, data.get('priority', 'High')),
              DEFAULT_USER))
        bug_id = cur.lastrowid
        blocks = get_link_type_id(db, 'Blocks')
        for target in (source_id, root_id):
            db.execute('INSERT INTO issue_link (link_type_id, source_issue_id, '
                       'destination_issue_id, created_by) VALUES (?, ?, ?, ?)',
                       (blocks, bug_id, target, DEFAULT_USER))
        db.commit()
        return jsonify({'id': bug_id, 'issue_key': key})
    except (KeyError, ValueError, sqlite3.IntegrityError) as e:
        db.rollback()
        return jsonify({'error': str(e)}), 400


@app.route('/api/issues/<int:iid>/available-actions')
def available_actions(iid):
    db = get_db()
    issue = get_issue(db, iid)
    t = get_type(db, issue['issue_type_id'])
    tname   = normalize_type_name(t['name'])
    current = get_status(db, issue['status_id'])['name']
    actions = [{'action': a, 'to': r['to']}
               for a, r in TRANSITIONS.get(tname, {}).items()
               if current in r['from']]
    return jsonify(actions)


@app.route('/api/issues/<int:iid>/transition', methods=['POST'])
def transition(iid):
    db = get_db()
    issue = get_issue(db, iid)
    payload = request.get_json() or {}
    action  = payload.get('action')
    if not action:
        return jsonify({'error': 'action required'}), 400
    try:
        new_status = apply_transition(db, issue, action, payload)
        return jsonify({'status': new_status})
    except (ValueError, sqlite3.IntegrityError) as e:
        db.rollback()
        return jsonify({'error': str(e)}), 400


@app.route('/api/issues/<int:iid>/comment', methods=['POST'])
def add_comment(iid):
    db = get_db()
    get_issue(db, iid)
    body = (request.get_json() or {}).get('body', '').strip()
    if not body:
        return jsonify({'error': 'body required'}), 400
    db.execute('INSERT INTO comment (issue_id, author_id, body) VALUES (?, ?, ?)',
               (iid, DEFAULT_USER, body))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/demo/seed', methods=['POST'])
def demo_seed():
    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM project WHERE project_key = 'MOB'").fetchone():
            db.execute("INSERT INTO project (project_key, name, description, lead_account_id) "
                       "VALUES ('MOB', 'Mobile App', 'Mobile application project', ?)", (DEFAULT_USER,))
        pid = db.execute("SELECT id FROM project WHERE project_key = 'MOB'").fetchone()['id']
        db.commit()

        def _deliverable():
            key = next_issue_key(db, pid)
            cur = db.execute('''INSERT INTO issue (issue_key, project_id, issue_type_id,
                                                    summary, description, status_id, priority_id, reporter_id)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (key, pid, get_type_id(db, 'Deliverable'),
                 'Passwordless OTP login v1.0',
                 'Deliver passwordless OTP login for mobile app v1.0',
                 get_status_id(db, 'Draft'), get_priority_id(db, 'High'), DEFAULT_USER))
            iid = cur.lastrowid
            db.execute('UPDATE issue SET deliverable_root_id = ? WHERE id = ?', (iid, iid))
            db.execute('INSERT INTO deliverable (issue_id, acceptance_criteria) VALUES (?, ?)',
                       (iid, '1. Request OTP\n2. Receive SMS\n3. Enter OTP within 5 minutes\n4. Login succeeds'))
            return iid

        def _ticket(parent_id, root_id, summary, kind='Story'):
            key = next_issue_key(db, pid)
            cur = db.execute('''INSERT INTO issue (issue_key, project_id, issue_type_id, parent_id,
                                                    deliverable_root_id, summary, status_id,
                                                    priority_id, reporter_id)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (key, pid, get_type_id(db, kind), parent_id, root_id,
                 summary, get_status_id(db, 'Draft'), get_priority_id(db, 'Medium'), DEFAULT_USER))
            return cur.lastrowid

        def _bug(source_id, root_id, summary):
            key = next_issue_key(db, pid)
            cur = db.execute('''INSERT INTO issue (issue_key, project_id, issue_type_id, parent_id,
                                                    deliverable_root_id, summary, status_id,
                                                    priority_id, reporter_id)
                                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)''',
                (key, pid, get_type_id(db, 'Bug'), root_id, summary,
                 get_status_id(db, 'Draft'), get_priority_id(db, 'High'), DEFAULT_USER))
            bug_id = cur.lastrowid
            blocks = get_link_type_id(db, 'Blocks')
            for tgt in (source_id, root_id):
                db.execute('INSERT INTO issue_link (link_type_id, source_issue_id, '
                           'destination_issue_id) VALUES (?, ?, ?)', (blocks, bug_id, tgt))
            return bug_id

        d_id = _deliverable()
        t1 = _ticket(d_id, d_id, 'Implement OTP request API')
        t2 = _ticket(d_id, d_id, 'Build OTP entry screen')
        t3 = _ticket(d_id, d_id, 'Write E2E login tests', 'Task')
        t4 = _ticket(d_id, d_id, 'Security review of OTP flow', 'Task')
        b1 = _bug(t1, d_id, 'OTP expires immediately (timezone mismatch)')
        b2 = _bug(t2, d_id, 'OTP field accepts letters')

        def do(iid, action, **kw):
            apply_transition(db, db.execute('SELECT * FROM issue WHERE id = ?', (iid,)).fetchone(),
                             action, kw)

        do(d_id, 'start')
        for tid in (t1, t2, t3, t4):
            do(tid, 'start')
        do(b1, 'start'); do(b1, 'fix', resolution_id=1); do(b1, 'verify'); do(b1, 'close', resolution_id=1)
        do(t1, 'done', resolution_id=1)
        do(b2, 'start'); do(b2, 'fix', resolution_id=1); do(b2, 'verify'); do(b2, 'close', resolution_id=1)
        do(t2, 'done', resolution_id=1); do(t3, 'done', resolution_id=1); do(t4, 'done', resolution_id=1)
        do(d_id, 'ready'); do(d_id, 'verify'); do(d_id, 'accept')

        db.commit()
        return jsonify({'ok': True, 'project_id': pid, 'deliverable_id': d_id})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 400


if __name__ == '__main__':
    init_db()
    app.run(host='127.0.0.1', port=5000, debug=True)