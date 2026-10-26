Keeping the tree exactly as written, with the tracker/REST terminology appended below.

---

```
DELIVERABLE   "Story-to-Video Pipeline — Full Film v1.0"
│
├── STORY  Phase 0 — Foundation
│    ├── SUB  0.1  Finalize JSON Schema v2.0
│    ├── SUB  0.2  Define Pydantic models
│    ├── SUB  0.3  Build validator layer
│    ├── SUB  0.4  Stand up ComfyUI API client
│    └── SUB  0.5  Pin Qwen 2B model
│
├── STORY  Phase 1 — Environment Plates
│    ├── SUB  1.1  LLM generates plate descriptions
│    ├── SUB  1.2  Code renders plate_t2i prompts
│    └── SUB  1.3  Generate plates via T2I
│
├── STORY  Phase 2 — LLM Story Decomposition
│    ├── SUB  2.1  Scene outline call
│    ├── SUB  2.2  Segment split call
│    ├── SUB  2.3  Clip breakdown call
│    └── SUB  2.4  Validate decomposition
│
├── STORY  Phase 3 — LLM Shot & Keyframe State
│    ├── SUB  3.1  Shot selection call
│    ├── SUB  3.2  First keyframe state call
│    ├── SUB  3.3  Middle & last delta call
│    ├── SUB  3.4  Code merges delta → full state
│    └── SUB  3.5  LLM motion prompt call
│
├── STORY  Phase 4 — Deterministic Rendering & QA
│    ├── SUB  4.1  Render all qwen_* prompts
│    ├── SUB  4.2  Generate keyframes in order
│    ├── SUB  4.3  QA keyframes (identity + framing)
│    ├── SUB  4.4  Retry / reset-to-plate
│    └── SUB  4.5  Periodic re-anchoring
│
├── STORY  Phase 5 — Video Generation & Assembly
│    ├── SUB  5.1  Render ltx_motion prompts
│    ├── SUB  5.2  Run LTX FML per clip
│    ├── SUB  5.3  Validate clip duration & frame rule
│    └── SUB  5.4  Concatenate final film
│
└── STORY  Phase 6 — Scale, Monitor & Iterate
     ├── SUB  6.1  Per-call prompt contracts
     ├── SUB  6.2  Automated retry loop
     ├── SUB  6.3  Run a full scene end-to-end
     ├── SUB  6.4  Measure & tune
     └── SUB  6.5  Scale to full film

BUGS (no parent — linked with "Blocks")
   e.g. "Identity score 0.72 < 0.8 on clip 3.2"  ──blocks──►  4.3  +  DELIVERABLE
        "LTX OOM on 12 GB at Q4 GGUF"            ──blocks──►  5.2  +  DELIVERABLE
        "LLM invalid JSON on scene outline (3/3)"──blocks──►  2.1  +  DELIVERABLE
```

---

# Tracker / REST terminology

### 1. Tree label → `issue_type.name` → workflow class

| Tree label | `issue_type.name` | `hierarchy_level` | `is_leaf` | `parent_id` | Normalized workflow | Created via |
|---|---|---|---|---|---|---|
| `DELIVERABLE` | `Deliverable` | 1 | 0 | `NULL` (self-root) | `Deliverable` | `POST /api/deliverables` |
| `STORY` | `Story` *(or `Task`)* | 2 | 0 | Deliverable id | `Ticket` | `POST /api/tickets` |
| `SUB` | `Sub-task` | 3 | 1 | Story or Task id | `Ticket` | `POST /api/tickets` |
| `BUG` | `Bug` | — | 1 | `NULL` (linked) | `Bug` | `POST /api/bugs` |

`Story` and `Task` are interchangeable at level 2 — `normalize_type_name()` folds both (plus `Sub-task`) into the single `Ticket` workflow. Only the display name differs.

### 2. How each node is created

**Deliverable** — `POST /api/deliverables`
```json
{ "project_id": 1, "summary": "...", "acceptance_criteria": "...",
  "description": "", "target_date": null }
```
Required: `summary`, `acceptance_criteria`. Side effect: sets `deliverable_root_id = id` on itself. Returns `{id, issue_key}`.

**Story / Task / Sub-task** — `POST /api/tickets`
```json
{ "project_id": 1, "parent_id": 7, "issue_type": "Story",
  "summary": "...", "description": "", "priority": "Medium" }
```
Required: `project_id`, `parent_id`, `summary`. `issue_type` defaults to `Story`. Inherits `deliverable_root_id` from parent. Runs `validate_hierarchy()`.

**Bug** — `POST /api/bugs`
```json
{ "source_ticket_id": 12, "summary": "...", "description": "", "priority": "High" }
```
Required: `source_ticket_id`, `summary`. `parent_id = NULL`. Side effect: inserts **two** `issue_link` rows with `link_type = 'Blocks'` — one to `source_ticket_id`, one to its `deliverable_root_id`. Returns `{id, issue_key}`.

### 3. Hierarchy rules enforced by `validate_hierarchy()`

| Rule | Effect |
|---|---|
| `Deliverable` and `Bug` cannot have a parent | rejected as `issue_type` in `/api/tickets` |
| Parent must not be `is_leaf` | `Sub-task` cannot parent anything |
| `parent.hierarchy_level < child.hierarchy_level` | Deliverable (1) < Story (2) < Sub-task (3) |
| Sub-task parent must be `Story` or `Task` | explicit check |
| Parent must be in same project | `parent.project_id == project_id` |

### 4. Workflow — `TRANSITIONS` keyed by normalized type

| Normalized type | Actions | Path |
|---|---|---|
| `Deliverable` | start, ready, verify, return, accept, cancel | Draft → In Progress → Ready for Acceptance → Criteria Verified → Accepted |
| `Ticket` | start, review, return, done, cancel | Draft → In Progress → In Review → Done |
| `Bug` | start, fix, reopen, verify, close, wontfix | Draft → In Progress → Fixed → Verified → Done |

**`done` gate for `Ticket`**: zero blocking bugs (`blocking_bugs_for_issue`), zero open sub-tasks (`open_subtasks`), `resolution_id` in payload.

**`ready` / `verify` / `accept` gate for `Deliverable`**: `compute_readiness()` — all counted tickets done, zero open bugs in subtree, acceptance criteria present.

### 5. Two fields that hold the tree together

| Field | Set by | Purpose |
|---|---|---|
| `issue.parent_id` | create endpoints | Visible tree edge (NULL for Deliverable + Bug) |
| `issue.deliverable_root_id` | create endpoints | Subtree tag — every node points at its Deliverable; drives `ticket_stats`, `blocking_bugs_in_subtree`, board queries |

### 6. Side-table for Bug blocking

`issue_link` row: `{ link_type_id → issue_link_type.name = 'Blocks', source_issue_id = bug, destination_issue_id = blocked, created_by }`. `issue_link_type.is_blocking = 1` makes the link count in both `blocking_bugs_for_issue()` and `blocking_bugs_in_subtree()`. That's why each Bug in the tree shows *two* arrows — the ticket **and** the Deliverable.

### 7. REST endpoints used at runtime

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | issue_types, statuses, priorities, resolutions, link_types, users |
| `GET /api/issues?project_id=&type=&category=&q=` | Flat list |
| `GET /api/issues/<id>` | Full detail — children, links, `blocking_bug_ids`, `readiness` (Deliverable only), comments, history |
| `GET /api/issues/<id>/available-actions` | Actions legal *right now*, derived from `TRANSITIONS` |
| `POST /api/issues/<id>/transition` | `{action, resolution_id?}` — the only way status changes |
| `POST /api/issues/<id>/comment` | `{body}` |
| `GET /api/projects/<pid>/board` | Deliverables + tickets + sub-tasks + bugs, assembled into board columns |
| `GET /api/dashboard` | Aggregated health, recent activity, blockers |

### 8. Quick glossary

| Term | Meaning in this tracker |
|---|---|
| `issue_key` | Human id `{project_key}-{n}`, e.g. `FILM-1`. Generated by `next_issue_key()` |
| `status` / `status_category` | Concrete state (Draft, Fixed, …) grouped into a category (`To Do`, `In Progress`, `Done`, …). The `Done` category drives every open/done check |
| `resolution` | Required at Ticket `done` and Bug `fix` / `close` / `wontfix` |
| `priority` | `issue.priority_id` → priority table; defaults to `Medium` for tickets, `High` for bugs |
| `DEFAULT_USER` | `u-admin` — stamped on every create / transition / comment |
| `parent_id` | Tree edge; NULL for Deliverable and Bug |
| `deliverable_root_id` | Subtree tag; always points at the owning Deliverable |
| `issue_link` / `Blocks` | How Bugs attach; `is_blocking = 1` makes them count in readiness |