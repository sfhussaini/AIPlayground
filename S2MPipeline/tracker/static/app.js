/* ============================================================
   Tracker — interactive dashboard + drill-down SPA
   ============================================================ */

/* ---------- State ---------- */
const S = {
    meta: {},
    projects: [],
    currentProjectId: parseInt(localStorage.getItem('tracker.projectId') || '0', 10),
    route: { name: 'dashboard', params: {} },
    dashboard: null,
    board: null,
    issue: null,
    issues: [],
    filter: { type: '', category: '', q: '' },
};

/* ---------- Icons ---------- */
const ICONS = {
    dashboard: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h5v5H2V2zm0 7h5v5H2V9zm7-7h5v5H9V2zm0 7h5v5H9V9z"/></svg>',
    board: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h3v12H2V2zm4.5 0h3v8h-3V2zM11 2h3v10h-3V2z"/></svg>',
    issues: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 3h12v1H2V3zm0 3h12v1H2V6zm0 3h12v1H2V9zm0 3h9v1H2v-1z"/></svg>',
    plus: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="2" fill="none"/></svg>',
    bug: '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="9" r="4"/><path d="M5 2l1 2h4l1-2M2 7h3M11 7h3M2 12h3M11 12h3"/></svg>',
    warn: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l7 13H1L8 1zm0 3v5h0zm0 6v2h0z"/></svg>',
    check: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M6.5 11.5L2 7l1.4-1.4 3.1 3.1 6.1-6.1L14 4z"/></svg>',
};

/* ---------- API ---------- */
async function api(path, opts = {}) {
    const res = await fetch('/api' + path, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
}

/* ---------- DOM helpers ---------- */
function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
        if (v == null) continue;
        if (k === 'class') node.className = v;
        else if (k === 'onclick') node.addEventListener('click', v);
        else if (k === 'onkeydown') node.addEventListener('keydown', v);
        else if (k === 'html') node.innerHTML = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
        else node.setAttribute(k, v);
    }
    for (const c of children) {
        if (c == null || c === false) continue;
        node.appendChild(typeof c === 'string' || typeof c === 'number'
            ? document.createTextNode(String(c)) : c);
    }
    return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function go(hash) { location.hash = hash; }

function toast(msg, kind = 'info', timeout = 3000) {
    const t = el('div', { class: 'toast ' + kind }, msg);
    document.getElementById('toast-root').appendChild(t);
    setTimeout(() => t.remove(), timeout);
}

/* ---------- Formatters ---------- */
function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso.replace(' ', 'T') + 'Z');
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso.replace(' ', 'T') + 'Z');
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

function timeAgo(iso) {
    if (!iso) return '';
    const d = new Date(iso.replace(' ', 'T') + 'Z').getTime();
    const s = Math.floor((Date.now() - d) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 604800) return Math.floor(s / 86400) + 'd ago';
    return fmtDate(iso);
}

/* ---------- Style helpers ---------- */
function statusCategoryStyle(cat) {
    switch (cat) {
        case 'To Do': return { bg: '#dfe1e6', color: '#42526e' };
        case 'In Progress': return { bg: '#deebff', color: '#0747a6' };
        case 'Done': return { bg: '#e3fcef', color: '#006644' };
        default: return { bg: '#f4f5f7', color: '#42526e' };
    }
}

function typeStyle(name) {
    switch (name) {
        case 'Deliverable': return { bg: '#eae6ff', color: '#5243aa' };
        case 'Epic': return { bg: '#eae6ff', color: '#5243aa' };
        case 'Story': return { bg: '#e3fcef', color: '#006644' };
        case 'Task': return { bg: '#deebff', color: '#0747a6' };
        case 'Bug': return { bg: '#ffecea', color: '#de350b' };
        case 'Sub-task': return { bg: '#e6fcff', color: '#0052cc' };
        default: return { bg: '#f4f5f7', color: '#42526e' };
    }
}

function statusPill(status) {
    if (!status) return el('span');
    const s = statusCategoryStyle(status.category_name);
    return el('span', { class: 'pill', style: { background: s.bg, color: s.color } },
        status.name);
}

function typeBadge(typeName) {
    const s = typeStyle(typeName);
    return el('span', { class: 'badge', style: { background: s.bg, color: s.color } }, typeName);
}

function progressBar(done, total, size = 'md') {
    const pct = total > 0 ? Math.round(100 * done / total) : 0;
    const cls = pct === 100 ? 'success' : (pct >= 50 ? '' : 'warning');
    return el('div', { class: 'progress', style: size === 'sm' ? { height: '4px' } : {} },
        el('div', { class: `progress-bar ${cls}`, style: { width: pct + '%' } }));
}

/* ============================================================
   Shell
   ============================================================ */
function renderShell() {
    const sidebar = document.getElementById('sidebar');
    clear(sidebar);
    sidebar.appendChild(el('div', { class: 'brand' },
        el('div', { class: 'brand-logo' }, 'T'),
        'Tracker'
    ));

    const navGroup = el('div', { class: 'nav-group' });
    navGroup.appendChild(el('div', { class: 'nav-group-title' }, 'Workspace'));
    for (const [route, icon, label] of [
        ['dashboard', ICONS.dashboard, 'Dashboard'],
        ['board', ICONS.board, 'Board'],
        ['issues', ICONS.issues, 'All Issues'],
    ]) {
        const active = S.route.name === route ? ' active' : '';
        const item = el('div', {
            class: 'nav-item' + active,
            onclick: () => go('#/' + route),
            html: icon + ' <span>' + label + '</span>',
        });
        navGroup.appendChild(item);
    }
    sidebar.appendChild(navGroup);

    if (S.projects.length) {
        const pg = el('div', { class: 'nav-group' });
        pg.appendChild(el('div', { class: 'nav-group-title' }, 'Projects'));
        for (const p of S.projects) {
            const active = (S.route.name === 'board' || S.route.name === 'issues')
                && S.currentProjectId === p.id ? ' active' : '';
            pg.appendChild(el('div', {
                class: 'nav-item' + active,
                onclick: () => { S.currentProjectId = p.id; saveProject(); go('#/board'); },
                title: p.name,
            }, el('span', { class: 'mono', style: { color: 'var(--text-muted)' } }, p.project_key),
                el('span', {
                    style: {
                        overflow: 'hidden', textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap'
                    }
                }, p.name)));
        }
        sidebar.appendChild(pg);
    }
}

function saveProject() {
    localStorage.setItem('tracker.projectId', String(S.currentProjectId || 0));
}

function renderTopbar() {
    const bar = document.getElementById('topbar');
    clear(bar);

    // Breadcrumb
    const crumbs = el('div', { class: 'topbar-breadcrumb' });
    if (S.route.name === 'dashboard') {
        crumbs.appendChild(el('span', { class: 'current' }, 'Dashboard'));
    } else if (S.route.name === 'board') {
        crumbs.appendChild(el('a', { onclick: () => go('#/dashboard') }, 'Dashboard'));
        crumbs.appendChild(el('span', { class: 'sep' }, '/'));
        const proj = S.projects.find(p => p.id === S.currentProjectId);
        crumbs.appendChild(el('span', { class: 'current' }, proj ? proj.name : 'Board'));
    } else if (S.route.name === 'issues') {
        crumbs.appendChild(el('a', { onclick: () => go('#/dashboard') }, 'Dashboard'));
        crumbs.appendChild(el('span', { class: 'sep' }, '/'));
        crumbs.appendChild(el('span', { class: 'current' }, 'All Issues'));
    } else if (S.route.name === 'issue') {
        crumbs.appendChild(el('a', { onclick: () => go('#/dashboard') }, 'Dashboard'));
        crumbs.appendChild(el('span', { class: 'sep' }, '/'));
        crumbs.appendChild(el('a', { onclick: () => go('#/board') },
            S.issue?.project?.name || 'Board'));
        crumbs.appendChild(el('span', { class: 'sep' }, '/'));
        crumbs.appendChild(el('span', { class: 'current mono' },
            S.issue?.issue_key || '…'));
    }
    bar.appendChild(crumbs);

    // Search
    const search = el('input', {
        class: 'topbar-search',
        placeholder: 'Search issues…  (press /)',
        value: S.filter.q,
        onkeydown: (e) => {
            if (e.key === 'Enter') {
                S.filter.q = e.target.value;
                go('#/issues');
            }
            if (e.key === 'Escape') { e.target.blur(); }
        },
    });
    search.addEventListener('input', e => { S.filter.q = e.target.value; });
    bar.appendChild(search);

    // Actions
    const actions = el('div', { class: 'topbar-actions' });
    if (S.route.name === 'board' && S.currentProjectId) {
        actions.appendChild(el('button', {
            class: 'btn btn-primary',
            html: ICONS.plus + ' <span>Deliverable</span>',
            onclick: openCreateDeliverable,
        }));
    } else if (S.route.name === 'dashboard') {
        actions.appendChild(el('button', {
            class: 'btn btn-secondary',
            onclick: seedDemo,
        }, 'Seed demo'));
        actions.appendChild(el('button', {
            class: 'btn btn-primary',
            html: ICONS.plus + ' <span>Project</span>',
            onclick: openCreateProject,
        }));
    }
    bar.appendChild(actions);
}

/* ============================================================
   Router
   ============================================================ */
async function navigate() {
    const hash = location.hash || '#/dashboard';
    const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean);

    let route;
    if (parts.length === 0 || parts[0] === 'dashboard') {
        route = { name: 'dashboard', params: {} };
    } else if (parts[0] === 'board') {
        route = { name: 'board', params: {} };
    } else if (parts[0] === 'issues') {
        route = { name: 'issues', params: {} };
    } else if (parts[0] === 'issue' && parts[1]) {
        route = { name: 'issue', params: { id: parseInt(parts[1], 10) } };
    } else {
        route = { name: 'dashboard', params: {} };
    }
    S.route = route;
    if (route.name !== 'issue') {
        S.issue = null;
    }

    renderShell();
    renderTopbar();

    try {
        if (route.name === 'dashboard') await renderDashboard();
        else if (route.name === 'board') await renderBoard();
        else if (route.name === 'issues') await renderIssues();
        else if (route.name === 'issue') await renderIssueDetail(route.params.id);
    } catch (err) {
        toast(err.message, 'error', 5000);
        document.getElementById('view').innerHTML =
            `<div class="empty"><div class="empty-title">${err.message}</div></div>`;
    }
}

/* ============================================================
   View: Dashboard
   ============================================================ */
async function renderDashboard() {
    const view = document.getElementById('view');
    clear(view);
    const data = await api('/dashboard');
    S.dashboard = data;

    const t = data.totals;

    // KPI grid
    const kpiGrid = el('div', { class: 'kpi-grid' });
    kpiGrid.appendChild(kpi('Projects', t.projects, '', '', 'info'));
    kpiGrid.appendChild(kpi('Deliverables', t.deliverables_open,
        `of ${t.deliverables_total} total`, '', 'info'));
    kpiGrid.appendChild(kpi('Tickets done', t.tickets_done,
        `of ${t.tickets_total} total`,
        t.tickets_total ? `${Math.round(100 * t.tickets_done / t.tickets_total)}% complete` : '',
        'success'));
    kpiGrid.appendChild(kpi('Open bugs', t.bugs_open,
        `of ${t.bugs_total} total`, '', 'warning'));
    kpiGrid.appendChild(kpi('Blocking bugs', t.bugs_blocking,
        'open, actively blocking', '', 'danger'));
    view.appendChild(kpiGrid);

    // Two-column row: health + activity
    const row = el('div', { class: 'dash-row' });

    // Project health card
    const healthCard = el('div', { class: 'card' });
    healthCard.appendChild(el('div', { class: 'card-header' },
        el('div', { class: 'card-title' }, 'Project health'),
        el('div', { class: 'text-sm text-muted' }, `${data.project_health.length} projects`)
    ));
    if (data.project_health.length) {
        const table = el('table', { class: 'health-table' });
        table.innerHTML = `
            <thead><tr>
                <th>Project</th>
                <th>Tickets</th>
                <th>Bugs</th>
                <th>Deliverables</th>
            </tr></thead>`;
        const tbody = el('tbody');
        for (const p of data.project_health) {
            const tr = el('tr', {
                onclick: () => {
                    S.currentProjectId = p.id; saveProject(); go('#/board');
                }, style: { cursor: 'pointer' }
            });

            tr.appendChild(el('td', {},
                el('div', { class: 'health-project-name' }, p.name),
                el('div', { class: 'health-project-key' }, p.project_key)
            ));
            tr.appendChild(el('td', {},
                el('div', { class: 'health-metric' },
                    el('span', { class: 'health-num' }, `${p.tickets_done}/${p.tickets_total}`),
                    el('div', { class: 'health-bar' }, progressBar(p.tickets_done, p.tickets_total, 'sm'))
                )
            ));
            tr.appendChild(el('td', {},
                el('span', { class: 'health-num' },
                    p.bugs_open > 0 ? `${p.bugs_open} open` : '—'),
                p.bugs_blocking > 0
                    ? el('span', {
                        class: 'pill', style: {
                            background: 'var(--danger-bg)', color: 'var(--danger)',
                            marginLeft: '6px', fontSize: '10px'
                        }
                    }, `${p.bugs_blocking} blocking`)
                    : null
            ));
            tr.appendChild(el('td', {},
                `${p.deliverables_open}/${p.deliverables_total} open`
            ));
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        healthCard.appendChild(table);
    } else {
        healthCard.appendChild(el('div', { class: 'empty' },
            el('div', { class: 'empty-title' }, 'No projects yet'),
            el('div', {}, 'Click “Project” to create your first one.')
        ));
    }
    row.appendChild(healthCard);

    // Recent activity card
    const actCard = el('div', { class: 'card' });
    actCard.appendChild(el('div', { class: 'card-header' },
        el('div', { class: 'card-title' }, 'Recent activity')));
    if (data.recent_activity.length) {
        const list = el('ul', { class: 'activity-list' });
        for (const a of data.recent_activity.slice(0, 15)) {
            list.appendChild(el('li', { class: 'activity-item' },
                el('div', {},
                    el('strong', {}, a.author_name || 'System'), ' ',
                    a.field === 'status' ? 'changed status of ' : `updated ${a.field} of `,
                    el('a', { href: '#/issue/' + a.issue_id, class: 'mono' }, a.issue_key)
                ),
                el('div', { class: 'meta' },
                    a.project_name, ' · ', timeAgo(a.created_at)),
                a.field === 'status' ? el('div', { class: 'change' },
                    el('code', {}, a.old_value || '—'), ' → ',
                    el('code', {}, a.new_value || '—')) : null
            ));
        }
        actCard.appendChild(list);
    } else {
        actCard.appendChild(el('div', { class: 'empty' },
            el('div', {}, 'No activity yet.')));
    }
    row.appendChild(actCard);
    view.appendChild(row);

    // Blockers card
    if (data.blockers.length) {
        const blockCard = el('div', { class: 'card' });
        blockCard.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title', style: { color: 'var(--danger)' } },
                `Blocking bugs (${data.blockers.length})`)));
        for (const b of data.blockers) {
            blockCard.appendChild(el('div', { class: 'blocker-item' },
                el('div', { class: 'blocker-icon', html: ICONS.warn }),
                el('div', { style: { flex: 1 } },
                    el('div', {},
                        el('a', {
                            href: '#/issue/' + b.id, class: 'mono', style: {
                                color: 'var(--danger)', fontWeight: 600
                            }
                        }, b.issue_key),
                        ' ', b.summary
                    ),
                    el('div', { class: 'blocker-meta' },
                        statusPill({ name: b.status_name, category_name: b.category_name }),
                        ' · ', b.project_name,
                        b.blocks_keys ? el('span', { class: 'blocks-label' },
                            ' · Blocks: ', el('span', { class: 'mono' }, b.blocks_keys)) : null
                    )
                )
            ));
        }
        view.appendChild(blockCard);
    }
}

function kpi(label, value, sub, sub2, kind = '') {
    return el('div', { class: 'kpi ' + kind },
        el('div', { class: 'kpi-label' }, label),
        el('div', { class: 'kpi-value' }, String(value)),
        sub ? el('div', { class: 'kpi-sub' }, sub) : null,
        sub2 ? el('div', { class: 'kpi-sub text-muted' }, sub2) : null,
    );
}

/* ============================================================
   View: Board
   ============================================================ */
async function renderBoard() {
    const view = document.getElementById('view');
    clear(view);

    if (!S.currentProjectId) {
        view.appendChild(emptyState('No project selected',
            'Pick a project in the sidebar, or create one from the Dashboard.'));
        return;
    }
    const data = await api(`/projects/${S.currentProjectId}/board`);
    S.board = data;

    const header = el('div', { class: 'board-header' },
        el('div', {},
            el('h1', { class: 'board-title' }, data.project.name),
            el('div', { class: 'board-sub' },
                data.project.description || '',
                data.deliverables.length
                    ? ` · ${data.deliverables.length} deliverable${data.deliverables.length === 1 ? '' : 's'}`
                    : '')
        )
    );
    view.appendChild(header);

    if (!data.deliverables.length) {
        view.appendChild(emptyState('No deliverables yet',
            'Create one to get started.'));
        const wrap = el('div', { style: { textAlign: 'center', marginTop: '16px' } },
            el('button', { class: 'btn btn-primary', onclick: openCreateDeliverable },
                'Create deliverable'));
        view.appendChild(wrap);
        return;
    }

    for (const d of data.deliverables) {
        view.appendChild(renderDeliverableCard(d));
    }
}

function emptyState(title, sub) {
    return el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, '📋'),
        el('div', { class: 'empty-title' }, title),
        el('div', {}, sub || ''));
}

function readinessSummary(d) {
    const r = d.readiness || {};
    const statusName = d.status?.name || '';
    if (statusName === 'Accepted') {
        return { label: 'Accepted', cls: 'success' };
    }
    if (statusName === 'Cancelled') {
        return { label: 'Cancelled', cls: 'danger' };
    }
    if (r.ready_for_acceptance) {
        return { label: 'Ready to accept', cls: 'success' };
    }
    if (r.ready_for_verification) {
        return { label: 'Ready for verification', cls: 'warning' };
    }
    const issues = [];
    if (r.open_blocking_bugs) issues.push(`${r.open_blocking_bugs} blocking bug${r.open_blocking_bugs === 1 ? '' : 's'}`);
    if (r.tickets_total - r.tickets_done) issues.push(`${r.tickets_total - r.tickets_done} open ticket${r.tickets_total - r.tickets_done === 1 ? '' : 's'}`);
    return { label: 'Blocked: ' + issues.join(', '), cls: 'danger' };
}

function renderDeliverableCard(d) {
    const card = el('div', { class: 'deliverable' });
    const r = d.readiness;
    const rs = readinessSummary(d);

    // Header
    const head = el('div', { class: 'deliverable-head' });
    const titleWrap = el('div', { style: { flex: 1, minWidth: 0 } });
    titleWrap.appendChild(el('div', { class: 'deliverable-title' },
        el('span', { class: 'issue-key' }, d.issue_key),
        el('a', { href: '#/issue/' + d.id, style: { color: 'inherit' } }, d.summary),
        statusPill(d.status),
        el('span', {
            class: `pill`, style: {
                background: rs.cls === 'success' ? 'var(--success-bg)' :
                    rs.cls === 'warning' ? 'var(--warning-bg)' : 'var(--danger-bg)',
                color: rs.cls === 'success' ? 'var(--success)' :
                    rs.cls === 'warning' ? 'var(--warning)' : 'var(--danger)',
                textTransform: 'none',
            }
        }, rs.label),
    ));

    const meta = el('div', { class: 'deliverable-meta' });
    meta.appendChild(el('span', {}, `${r.tickets_done}/${r.tickets_total} tickets done`));
    meta.appendChild(el('span', {}, `${d.bugs.length} bug${d.bugs.length === 1 ? '' : 's'}`));
    if (r.open_blocking_bugs) {
        meta.appendChild(el('span', { class: 'text-danger' },
            `${r.open_blocking_bugs} blocking`));
    }
    titleWrap.appendChild(meta);
    titleWrap.appendChild(el('div', { class: 'deliverable-progress' },
        progressBar(r.tickets_done, r.tickets_total)));

    const actions = el('div', { class: 'actions' });
    actions.appendChild(el('button', {
        class: 'btn btn-primary btn-sm',
        onclick: () => openTransition(d.id)
    }, 'Actions'));
    actions.appendChild(el('button', {
        class: 'btn btn-secondary btn-sm',
        html: ICONS.plus + ' <span>Ticket</span>',
        onclick: () => openCreateTicket(d.id)
    }));
    head.appendChild(titleWrap);
    head.appendChild(actions);
    card.appendChild(head);

    // Criteria
    if (d.deliverable && d.deliverable.acceptance_criteria) {
        card.appendChild(el('div', { class: 'deliverable-criteria' },
            el('strong', {}, 'Acceptance criteria'),
            d.deliverable.acceptance_criteria
        ));
    }

    // Tickets
    card.appendChild(el('div', { class: 'section-title' },
        el('span', {}, `Tickets (${d.tickets.length})`)));
    if (!d.tickets.length) {
        card.appendChild(el('div', {
            style: {
                padding: '12px 20px',
                color: 'var(--text-muted)', fontSize: '13px'
            }
        }, 'No tickets yet.'));
    } else {
        for (const tk of d.tickets) {
            card.appendChild(renderTicketRow(tk));
            for (const st of tk.subtasks) {
                card.appendChild(el('div', { class: 'subtask-row' },
                    el('span', { class: 'issue-key' }, st.issue_key),
                    el('a', { href: '#/issue/' + st.id, style: { color: 'inherit' } }, st.summary),
                    el('span', { class: 'text-muted' }, '—'),
                    el('span', { class: 'text-muted' }, statusName(st.status_id))
                ));
            }
        }
    }

    // Bugs
    card.appendChild(el('div', { class: 'section-title' },
        el('span', {}, `Bugs (${d.bugs.length})`)));
    if (!d.bugs.length) {
        card.appendChild(el('div', {
            style: {
                padding: '12px 20px',
                color: 'var(--text-muted)', fontSize: '13px'
            }
        }, 'No bugs reported.'));
    } else {
        for (const b of d.bugs) card.appendChild(renderBugRow(b));
    }

    return card;
}

function statusName(statusId) {
    const s = S.meta.statuses.find(x => x.id === statusId);
    return s ? s.name : '—';
}

function renderTicketRow(tk) {
    const row = el('div', { class: 'ticket-row' });
    const main = el('div', { class: 'ticket-main' });
    main.appendChild(el('div', { class: 'ticket-title' },
        el('span', { class: 'issue-key' }, tk.issue.issue_key),
        el('a', { href: '#/issue/' + tk.issue.id }, tk.issue.summary)));
    const meta = el('div', { class: 'ticket-meta' });
    meta.appendChild(typeBadge(S.meta.issue_types.find(t => t.id === tk.issue.issue_type_id)?.name || 'Ticket'));
    meta.appendChild(statusPill(tk.status));
    if (tk.blocking_bug_ids.length) {
        meta.appendChild(el('span', {
            class: 'pill',
            style: { background: 'var(--danger-bg)', color: 'var(--danger)' }
        },
            `${tk.blocking_bug_ids.length} blocking`));
    }
    main.appendChild(meta);
    const actions = el('div', { class: 'actions' });
    actions.appendChild(el('button', {
        class: 'btn btn-secondary btn-sm',
        onclick: () => openTransition(tk.issue.id)
    }, 'Actions'));
    actions.appendChild(el('button', {
        class: 'btn btn-ghost btn-sm',
        html: ICONS.bug, title: 'Report bug',
        onclick: () => openCreateBug(tk.issue.id)
    }));
    row.appendChild(main);
    row.appendChild(actions);
    return row;
}

function renderBugRow(b) {
    const isOpen = !['Done', 'Cancelled'].includes(b.status?.name || '');
    const row = el('div', { class: 'bug-row' + (isOpen && b.blocks.length ? ' blocking' : '') });
    const main = el('div', { class: 'ticket-main' });
    main.appendChild(el('div', { class: 'ticket-title' },
        el('span', { class: 'issue-key' }, b.issue.issue_key),
        el('a', { href: '#/issue/' + b.issue.id }, b.issue.summary)));
    const meta = el('div', { class: 'ticket-meta' });
    meta.appendChild(typeBadge('Bug'));
    meta.appendChild(statusPill(b.status));
    if (b.issue.resolution_id) {
        const r = S.meta.resolutions.find(x => x.id === b.issue.resolution_id);
        if (r) meta.appendChild(el('span', {}, 'Resolution: ' + r.name));
    }
    if (b.blocks.length) {
        meta.appendChild(el('span', { style: { color: 'var(--danger)' } },
            'Blocks: ' + b.blocks.map(x => x.issue_key).join(', ')));
    }
    main.appendChild(meta);
    const actions = el('div', { class: 'actions' });
    actions.appendChild(el('button', {
        class: 'btn btn-secondary btn-sm',
        onclick: () => openTransition(b.issue.id)
    }, 'Actions'));
    row.appendChild(main);
    row.appendChild(actions);
    return row;
}

/* ============================================================
   View: All Issues
   ============================================================ */
async function renderIssues() {
    const view = document.getElementById('view');
    clear(view);

    const filterBar = el('div', { class: 'filter-bar' });

    // Type filter
    const typeChips = el('div', { class: 'filter-chips' });
    for (const t of ['', 'Deliverable', 'Story', 'Task', 'Bug']) {
        const active = S.filter.type === t ? ' active' : '';
        typeChips.appendChild(el('button', {
            class: 'chip' + active,
            onclick: () => { S.filter.type = t; renderIssues(); },
        }, t || 'All types'));
    }
    filterBar.appendChild(typeChips);

    // Category filter
    const catChips = el('div', { class: 'filter-chips' });
    for (const c of ['', 'To Do', 'In Progress', 'Done']) {
        const active = S.filter.category === c ? ' active' : '';
        catChips.appendChild(el('button', {
            class: 'chip' + active,
            onclick: () => { S.filter.category = c; renderIssues(); },
        }, c || 'Any status'));
    }
    filterBar.appendChild(catChips);
    view.appendChild(filterBar);

    // Fetch
    const params = new URLSearchParams();
    if (S.currentProjectId) params.set('project_id', S.currentProjectId);
    if (S.filter.type) params.set('type', S.filter.type);
    if (S.filter.category) params.set('category', S.filter.category);
    if (S.filter.q) params.set('q', S.filter.q);
    const issues = await api('/issues?' + params.toString());
    S.issues = issues;

    if (!issues.length) {
        view.appendChild(emptyState('No issues match',
            'Try clearing filters or selecting a different project.'));
        return;
    }

    const card = el('div', { class: 'card' });
    const table = el('table', { class: 'issue-table' });
    table.innerHTML = `
        <thead><tr>
            <th class="col-key">Key</th>
            <th class="col-type">Type</th>
            <th>Summary</th>
            <th class="col-status">Status</th>
            <th class="col-priority">Priority</th>
            <th>Project</th>
            <th>Updated</th>
        </tr></thead>`;
    const tbody = el('tbody');
    for (const i of issues) {
        const tr = el('tr', { onclick: () => go('#/issue/' + i.id) });
        tr.appendChild(el('td', { class: 'mono' }, i.issue_key));
        tr.appendChild(el('td', {}, typeBadge(i.type_name)));
        tr.appendChild(el('td', {}, i.summary));
        tr.appendChild(el('td', {}, statusPill({
            name: i.status_name, category_name: i.category_name
        })));
        tr.appendChild(el('td', {}, i.priority_name || '—'));
        tr.appendChild(el('td', { class: 'text-sm text-sub' }, i.project_name));
        tr.appendChild(el('td', { class: 'text-sm text-sub' }, timeAgo(i.updated_at)));
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    card.appendChild(table);
    view.appendChild(card);
}

/* ============================================================
   View: Issue Detail (drill-down)
   ============================================================ */
async function renderIssueDetail(id) {
    const view = document.getElementById('view');
    clear(view);
    const d = await api('/issues/' + id);
    S.issue = d;

    // Header card
    const header = el('div', { class: 'detail-header' });
    const top = el('div', { class: 'flex gap-8', style: { alignItems: 'center' } });
    top.appendChild(el('span', { class: 'issue-key', style: { fontSize: '13px' } }, d.issue_key));
    top.appendChild(typeBadge(d.type?.name || 'Issue'));
    header.appendChild(top);

    header.appendChild(el('h1', { class: 'detail-title' }, d.summary));

    const metaRow = el('div', { class: 'detail-meta-row' });
    metaRow.appendChild(metaItem('Status', statusPill(d.status)));
    if (d.priority) metaRow.appendChild(metaItem('Priority', d.priority.name));
    if (d.resolution) metaRow.appendChild(metaItem('Resolution', d.resolution.name));
    metaRow.appendChild(metaItem('Project', d.project?.name || '—'));
    if (d.parent) {
        metaRow.appendChild(metaItem('Parent',
            el('a', { href: '#/issue/' + d.parent.id, class: 'mono' }, d.parent.issue_key)));
    }
    if (d.deliverable_root) {
        metaRow.appendChild(metaItem('Deliverable',
            el('a', { href: '#/issue/' + d.deliverable_root.id, class: 'mono' },
                d.deliverable_root.issue_key)));
    }
    header.appendChild(metaRow);

    // Actions row
    const actionRow = el('div', { class: 'flex gap-8 mt-16', style: { flexWrap: 'wrap' } });
    actionRow.appendChild(el('button', {
        class: 'btn btn-primary', onclick: () => openTransition(d.id),
    }, 'Actions'));
    if (d.type?.name === 'Deliverable') {
        actionRow.appendChild(el('button', {
            class: 'btn btn-secondary',
            html: ICONS.plus + ' <span>Ticket</span>',
            onclick: () => openCreateTicket(d.id),
        }));
    } else if (['Story', 'Task', 'Sub-task'].includes(d.type.name)) {
        actionRow.appendChild(el('button', {
            class: 'btn btn-secondary',
            html: ICONS.bug + ' <span>Report bug</span>',
            onclick: () => openCreateBug(d.id),
        }));
    }
    if (d.deliverable_root) {
        actionRow.appendChild(el('button', {
            class: 'btn btn-ghost',
            onclick: () => go('#/board'),
        }, '← Back to board'));
    }
    header.appendChild(actionRow);
    view.appendChild(header);

    // Grid
    const grid = el('div', { class: 'detail-grid' });
    const main = el('div', { class: 'detail-main' });
    const side = el('div', { class: 'detail-side' });
    grid.appendChild(main);
    grid.appendChild(side);

    // Description
    if (d.description) {
        const c = el('div', { class: 'card' });
        c.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title' }, 'Description')));
        c.appendChild(el('div', { class: 'card-pad' },
            el('div', { class: 'description' }, d.description)));
        main.appendChild(c);
    }

    // Acceptance criteria (Deliverable)
    if (d.deliverable && d.deliverable.acceptance_criteria) {
        const c = el('div', { class: 'card' });
        c.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title' }, 'Acceptance criteria')));
        c.appendChild(el('div', { class: 'card-pad' },
            el('div', { class: 'criteria-block' }, d.deliverable.acceptance_criteria)));
        main.appendChild(c);
    }

    // Readiness (Deliverable)
    if (d.readiness) {
        const r = d.readiness;
        const c = el('div', { class: 'card' });
        c.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title' }, 'Readiness')));
        const block = el('div', { class: 'readiness-block' });
        const statusLabel = r.ready_for_acceptance
            ? { text: 'Ready to accept', color: 'var(--success)', bg: 'var(--success-bg)' }
            : r.ready_for_verification
                ? { text: 'Ready for verification', color: 'var(--warning)', bg: 'var(--warning-bg)' }
                : { text: 'Blocked', color: 'var(--danger)', bg: 'var(--danger-bg)' };
        block.appendChild(el('div', {
            class: 'readiness-status', style: {
                background: statusLabel.bg, color: statusLabel.color,
                padding: '8px 12px', borderRadius: '3px'
            }
        },
            statusLabel.text));
        const metrics = el('div', { class: 'readiness-metrics' });
        metrics.appendChild(readinessMetric('Tickets done',
            `${r.tickets_done}/${r.tickets_total}`));
        metrics.appendChild(readinessMetric('Blocking bugs', r.open_blocking_bugs,
            r.open_blocking_bugs > 0 ? 'danger' : 'success'));
        metrics.appendChild(readinessMetric('Criteria verified',
            d.deliverable && d.deliverable.criteria_verified_at ? 'Yes' : 'No',
            d.deliverable && d.deliverable.criteria_verified_at ? 'success' : 'warning'));
        metrics.appendChild(readinessMetric('Accepted',
            d.deliverable && d.deliverable.accepted_at ? 'Yes' : 'No',
            d.deliverable && d.deliverable.accepted_at ? 'success' : ''));
        block.appendChild(metrics);
        c.appendChild(el('div', { class: 'card-pad' }, block));
        main.appendChild(c);
    }

    // Children (sub-tasks / tickets under deliverable)
    if (d.children && d.children.length) {
        const c = el('div', { class: 'card' });
        c.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title' }, `Children (${d.children.length})`)));
        for (const ch of d.children) {
            c.appendChild(el('div', {
                style: {
                    padding: '10px 18px',
                    borderBottom: '1px solid var(--border)', fontSize: '13px'
                }
            },
                el('a', {
                    href: '#/issue/' + ch.id, class: 'mono', style: {
                        marginRight: '8px', color: 'var(--text-sub)'
                    }
                }, ch.issue_key),
                el('a', { href: '#/issue/' + ch.id }, ch.summary),
                el('span', { class: 'text-muted', style: { marginLeft: '10px' } },
                    '— ' + ch.status_name)
            ));
        }
        main.appendChild(c);
    }

    // Links
    if (d.links.length) {
        const c = el('div', { class: 'card' });
        c.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title' }, `Links (${d.links.length})`)));
        const list = el('ul', { class: 'link-list', style: { padding: '8px 18px' } });
        for (const l of d.links) {
            let html;
            if (l.source_issue_id === d.id) {
                html = [
                    el('span', { class: 'link-type' }, l.outward_description + ' '),
                    el('a', {
                        href: '#/issue/' + l.destination_issue_id, class: 'mono',
                        style: { marginRight: '6px' }
                    }, l.dst_key),
                    l.dst_summary,
                ];
            } else {
                html = [
                    el('span', { class: 'link-type' }, l.inward_description + ' '),
                    el('a', {
                        href: '#/issue/' + l.source_issue_id, class: 'mono',
                        style: { marginRight: '6px' }
                    }, l.src_key),
                    l.src_summary,
                ];
            }
            list.appendChild(el('li', { class: 'link-item' }, ...html));
        }
        c.appendChild(list);
        main.appendChild(c);
    }

    // Comments
    const commentsCard = el('div', { class: 'card' });
    commentsCard.appendChild(el('div', { class: 'card-header' },
        el('div', { class: 'card-title' }, `Comments (${d.comments.length})`)));
    const cBody = el('div', { class: 'card-pad' });
    for (const cm of d.comments) {
        cBody.appendChild(el('div', { class: 'comment' },
            el('div', { class: 'comment-head' },
                el('span', {}, cm.author_name || cm.author_id),
                el('span', { class: 'comment-time' }, timeAgo(cm.created_at))),
            el('div', { class: 'comment-body' }, cm.body)));
    }
    // Add comment form
    const form = el('div', { class: 'comment-form' });
    const ta = el('textarea', { placeholder: 'Add a comment…' });
    form.appendChild(ta);
    form.appendChild(el('div', { class: 'comment-form-actions' },
        el('button', {
            class: 'btn btn-primary btn-sm', onclick: async () => {
                const body = ta.value.trim();
                if (!body) return;
                try {
                    await api(`/issues/${d.id}/comment`, { method: 'POST', body: { body } });
                    ta.value = '';
                    toast('Comment added', 'success');
                    renderIssueDetail(d.id);
                } catch (e) { toast(e.message, 'error'); }
            }
        }, 'Post comment')));
    cBody.appendChild(form);
    commentsCard.appendChild(cBody);
    main.appendChild(commentsCard);

    // Sidebar — metadata
    const metaCard = el('div', { class: 'card' });
    metaCard.appendChild(el('div', { class: 'card-header' },
        el('div', { class: 'card-title' }, 'Details')));
    const mBody = el('div', { class: 'card-pad' });
    mBody.appendChild(fieldRow('Type', d.type.name));
    mBody.appendChild(fieldRow('Status', statusPill(d.status)));
    mBody.appendChild(fieldRow('Priority', d.priority ? d.priority.name : '—'));
    if (d.resolution) mBody.appendChild(fieldRow('Resolution', d.resolution.name));
    mBody.appendChild(fieldRow('Reporter', d.reporter ? d.reporter.display_name : '—'));
    mBody.appendChild(fieldRow('Assignee', d.assignee ? d.assignee.display_name : '—'));
    mBody.appendChild(fieldRow('Created', fmtDateTime(d.created_at)));
    mBody.appendChild(fieldRow('Updated', fmtDateTime(d.updated_at)));
    if (d.resolved_at) mBody.appendChild(fieldRow('Resolved', fmtDateTime(d.resolved_at)));
    if (d.verified_at) mBody.appendChild(fieldRow('Verified', fmtDateTime(d.verified_at)));
    if (d.deliverable && d.deliverable.target_date) {
        mBody.appendChild(fieldRow('Target date', d.deliverable.target_date));
    }
    metaCard.appendChild(mBody);
    side.appendChild(metaCard);

    // History
    if (d.history.length) {
        const hCard = el('div', { class: 'card', style: { marginTop: '16px' } });
        hCard.appendChild(el('div', { class: 'card-header' },
            el('div', { class: 'card-title' }, 'History')));
        const ul = el('ul', { class: 'history-list', style: { padding: '8px 18px' } });
        for (const h of d.history) {
            ul.appendChild(el('li', { class: 'history-item' },
                el('div', { class: 'history-time' }, timeAgo(h.created_at)),
                el('div', { class: 'history-body' },
                    el('strong', {}, h.author_name || h.author_id || 'System'),
                    ' — ',
                    h.field === 'status'
                        ? `status: ${h.old_value || '—'} → ${h.new_value || '—'}`
                        : `${h.field}: ${h.old_value || '—'} → ${h.new_value || '—'}`
                )));
        }
        hCard.appendChild(ul);
        side.appendChild(hCard);
    }

    view.appendChild(grid);
    renderTopbar();   // refresh breadcrumb now that S.issue is set
}

function metaItem(label, value) {
    return el('div', { class: 'detail-meta-item' },
        el('span', { class: 'detail-meta-label' }, label + ':'),
        value);
}

function fieldRow(label, value) {
    return el('div', { class: 'field-row' },
        el('div', { class: 'field-label' }, label),
        el('div', { class: 'field-value' }, value));
}

function readinessMetric(label, value, kind = '') {
    const color = kind === 'danger' ? 'var(--danger)'
        : kind === 'success' ? 'var(--success)'
            : kind === 'warning' ? 'var(--warning)' : 'var(--text)';
    return el('div', { class: 'readiness-metric' },
        el('div', { class: 'label' }, label),
        el('div', { class: 'value', style: { color } }, String(value)));
}

/* ============================================================
   Modals
   ============================================================ */
function openModal(title, bodyFn, footFn) {
    const root = document.getElementById('modal-root');
    clear(root);
    const backdrop = el('div', {
        class: 'modal-backdrop',
        onclick: (e) => { if (e.target === backdrop) closeModal(); }
    });
    const modal = el('div', { class: 'modal' });
    const header = el('div', { class: 'modal-header' },
        el('span', {}, title),
        el('button', { class: 'modal-close', onclick: closeModal }, '×'));
    modal.appendChild(header);

    const body = el('div', { class: 'modal-body' });
    bodyFn(body);
    modal.appendChild(body);

    if (footFn) {
        const foot = el('div', { class: 'modal-footer' });
        footFn(foot, body);
        modal.appendChild(foot);
    }

    backdrop.appendChild(modal);
    root.appendChild(backdrop);
}

function closeModal() {
    document.getElementById('modal-root').innerHTML = '';
}

function formGroup(label, input) {
    return el('div', { class: 'form-group' },
        el('label', { class: 'form-label' }, label), input);
}

function errorBox(msg) {
    return el('div', { class: 'form-error' }, msg);
}

/* ---------- Create project ---------- */
function openCreateProject() {
    openModal('New project', (body) => {
        body.appendChild(formGroup('Project key',
            el('input', { class: 'form-input', id: 'in-key', placeholder: 'MOB', maxlength: '10' })));
        body.appendChild(formGroup('Project name',
            el('input', { class: 'form-input', id: 'in-name', placeholder: 'Mobile App' })));
        body.appendChild(formGroup('Description (optional)',
            el('textarea', { class: 'form-textarea', id: 'in-desc' })));
    }, (foot, body) => {
        foot.appendChild(el('button', {
            class: 'btn btn-secondary',
            onclick: closeModal
        }, 'Cancel'));
        foot.appendChild(el('button', {
            class: 'btn btn-primary', onclick: async () => {
                try {
                    const key = document.getElementById('in-key').value.trim().toUpperCase();
                    const name = document.getElementById('in-name').value.trim();
                    const desc = document.getElementById('in-desc').value;
                    if (!key || !name) throw new Error('Key and name are required');
                    const res = await api('/projects', {
                        method: 'POST',
                        body: { project_key: key, name, description: desc }
                    });
                    S.currentProjectId = res.id;
                    saveProject();
                    closeModal();
                    toast(`Project ${res.project_key} created`, 'success');
                    await loadProjects();
                    go('#/board');
                    navigate();
                } catch (e) {
                    body.insertBefore(errorBox(e.message), body.firstChild);
                }
            }
        }, 'Create'));
    });
}

/* ---------- Create deliverable ---------- */
function openCreateDeliverable() {
    if (!S.currentProjectId) return toast('Select a project first', 'error');
    openModal('New deliverable', (body) => {
        body.appendChild(formGroup('Summary',
            el('input', {
                class: 'form-input', id: 'in-summary',
                placeholder: 'Passwordless OTP login v1.0'
            })));
        body.appendChild(formGroup('Acceptance criteria',
            el('textarea', {
                class: 'form-textarea', id: 'in-criteria',
                placeholder: '1. Request OTP\n2. Receive SMS\n3. Enter OTP within 5 minutes\n4. Login succeeds'
            })));
        body.appendChild(formGroup('Description (optional)',
            el('textarea', { class: 'form-textarea', id: 'in-desc' })));
    }, (foot, body) => {
        foot.appendChild(el('button', {
            class: 'btn btn-secondary',
            onclick: closeModal
        }, 'Cancel'));
        foot.appendChild(el('button', {
            class: 'btn btn-primary', onclick: async () => {
                try {
                    const summary = document.getElementById('in-summary').value.trim();
                    const criteria = document.getElementById('in-criteria').value.trim();
                    const desc = document.getElementById('in-desc').value;
                    if (!summary || !criteria) throw new Error('Summary and criteria are required');
                    const res = await api('/deliverables', {
                        method: 'POST', body: {
                            project_id: S.currentProjectId,
                            summary, acceptance_criteria: criteria, description: desc,
                        }
                    });
                    closeModal();
                    toast(`Deliverable ${res.issue_key} created`, 'success');
                    navigate();
                } catch (e) {
                    body.insertBefore(errorBox(e.message), body.firstChild);
                }
            }
        }, 'Create'));
    });
}

/* ---------- Create ticket ---------- */
function openCreateTicket(parentId) {
    openModal('New ticket', (body) => {
        body.appendChild(formGroup('Type',
            el('select', { class: 'form-select', id: 'in-type' },
                el('option', { value: 'Story' }, 'Story'),
                el('option', { value: 'Task' }, 'Task'))));
        body.appendChild(formGroup('Summary',
            el('input', { class: 'form-input', id: 'in-summary' })));
        body.appendChild(formGroup('Priority',
            el('select', { class: 'form-select', id: 'in-priority' },
                ...S.meta.priorities.map(p =>
                    el('option', { value: p.name, selected: p.name === 'Medium' ? '' : null },
                        p.name)))));
    }, (foot, body) => {
        foot.appendChild(el('button', {
            class: 'btn btn-secondary',
            onclick: closeModal
        }, 'Cancel'));
        foot.appendChild(el('button', {
            class: 'btn btn-primary', onclick: async () => {
                try {
                    const summary = document.getElementById('in-summary').value.trim();
                    const type = document.getElementById('in-type').value;
                    const priority = document.getElementById('in-priority').value;
                    if (!summary) throw new Error('Summary is required');
                    const res = await api('/tickets', {
                        method: 'POST', body: {
                            project_id: S.currentProjectId,
                            parent_id: parentId, issue_type: type, summary, priority,
                        }
                    });
                    closeModal();
                    toast(`Ticket ${res.issue_key} created`, 'success');
                    navigate();
                } catch (e) {
                    body.insertBefore(errorBox(e.message), body.firstChild);
                }
            }
        }, 'Create'));
    });
}

/* ---------- Create bug ---------- */
function openCreateBug(sourceId) {
    openModal('Report bug', (body) => {
        body.appendChild(formGroup('Summary',
            el('input', {
                class: 'form-input', id: 'in-summary',
                placeholder: 'What went wrong?'
            })));
        body.appendChild(formGroup('Description / repro steps',
            el('textarea', { class: 'form-textarea', id: 'in-desc' })));
        body.appendChild(formGroup('Priority',
            el('select', { class: 'form-select', id: 'in-priority' },
                ...S.meta.priorities.map(p =>
                    el('option', { value: p.name, selected: p.name === 'High' ? '' : null },
                        p.name)))));
        body.appendChild(el('div', { class: 'text-sm text-muted', style: { marginTop: '12px' } },
            'The bug will be auto-linked as “Blocks” to this ticket and its deliverable.'));
    }, (foot, body) => {
        foot.appendChild(el('button', {
            class: 'btn btn-secondary',
            onclick: closeModal
        }, 'Cancel'));
        foot.appendChild(el('button', {
            class: 'btn btn-primary', onclick: async () => {
                try {
                    const summary = document.getElementById('in-summary').value.trim();
                    const desc = document.getElementById('in-desc').value;
                    const priority = document.getElementById('in-priority').value;
                    if (!summary) throw new Error('Summary is required');
                    const res = await api('/bugs', {
                        method: 'POST', body: {
                            source_ticket_id: sourceId, summary, description: desc, priority,
                        }
                    });
                    closeModal();
                    toast(`Bug ${res.issue_key} created`, 'success');
                    navigate();
                } catch (e) {
                    body.insertBefore(errorBox(e.message), body.firstChild);
                }
            }
        }, 'Create'));
    });
}

/* ---------- Transition ---------- */
async function openTransition(issueId) {
    let actions;
    try { actions = await api(`/issues/${issueId}/available-actions`); }
    catch (e) { return toast(e.message, 'error'); }

    if (!actions.length) {
        return toast('No actions available from this state', 'error');
    }

    const needsResolution = actions.some(a =>
        ['done', 'fix', 'close', 'wontfix'].includes(a.action));

    openModal('Actions', (body) => {
        if (needsResolution) {
            body.appendChild(formGroup('Resolution (for done / fix / close)',
                el('select', { class: 'form-select', id: 'in-resolution' },
                    el('option', { value: '' }, '— choose —'),
                    ...S.meta.resolutions.map(r =>
                        el('option', { value: r.id }, r.name)))));
        }

        const list = el('div', { class: 'action-list' });
        for (const a of actions) {
            const label = a.action.replace(/^\w/, c => c.toUpperCase());
            const isPrimary = ['accept', 'done', 'close'].includes(a.action);
            const btn = el('button', {
                class: 'action-btn' + (isPrimary ? ' primary' : ''),
                onclick: async () => {
                    try {
                        const payload = { action: a.action };
                        if (['done', 'fix', 'close', 'wontfix'].includes(a.action)) {
                            const sel = document.getElementById('in-resolution');
                            if (!sel || !sel.value) throw new Error('Please choose a resolution');
                            payload.resolution_id = parseInt(sel.value, 10);
                        }
                        const res = await api(`/issues/${issueId}/transition`, {
                            method: 'POST', body: payload
                        });
                        closeModal();
                        toast(`→ ${res.status}`, 'success');
                        navigate();
                    } catch (e) {
                        const body = document.querySelector('.modal-body');
                        const existing = body.querySelector('.form-error');
                        if (existing) existing.remove();
                        body.insertBefore(errorBox(e.message), body.firstChild);
                    }
                },
            }, el('span', {}, label),
                el('span', { class: 'to-status' }, '→ ' + a.to));
            list.appendChild(btn);
        }
        body.appendChild(list);
    });
}

/* ---------- Seed demo ---------- */
async function seedDemo() {
    try {
        const res = await api('/demo/seed', { method: 'POST', body: {} });
        S.currentProjectId = res.project_id;
        saveProject();
        toast('Demo data seeded', 'success');
        await loadProjects();
        navigate();
    } catch (e) { toast(e.message, 'error'); }
}

/* ============================================================
   Boot
   ============================================================ */
async function loadMeta() {
    S.meta = await api('/meta');
}

async function loadProjects() {
    S.projects = await api('/projects');
    if (!S.currentProjectId && S.projects.length) {
        S.currentProjectId = S.projects[0].id;
        saveProject();
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    // Global key: / focuses search
    document.addEventListener('keydown', (e) => {
        if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
            e.preventDefault();
            document.querySelector('.topbar-search')?.focus();
        }
        if (e.key === 'Escape') {
            closeModal();
        }
    });

    try {
        await loadMeta();
        await loadProjects();
    } catch (e) {
        toast('Cannot load data: ' + e.message, 'error', 5000);
        return;
    }

    if (!location.hash) go('#/dashboard');
    window.addEventListener('hashchange', navigate);
    navigate();
});