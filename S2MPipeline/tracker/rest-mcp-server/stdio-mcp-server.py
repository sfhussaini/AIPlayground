#!/usr/bin/env python3
"""
STDIO-based MCP Server for the Tracker REST API.

Exposes the deliverable / ticket / bug workflow as MCP tools. Each tool
maps to one endpoint on the Flask Tracker API (app.py from the previous
step). Workflow invariants — hierarchy, blocking-bug gating, acceptance
gates — are enforced by the API, not by this server.

Environment:
    TRACKER_BASE_URL   Base URL of the Tracker API (default http://127.0.0.1:5000)
    TRACKER_TIMEOUT    HTTP timeout seconds (default 30)

Transport: stdio.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Use FastMCP (official SDK). Fall back to MCPServer if the installed
# SDK version names the class that way (as in the workshop PDF).
try:
    from mcp.server.fastmcp import FastMCP as _MCP
except ImportError:                                # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _MCP  # type: ignore


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
BASE_URL = os.environ.get("TRACKER_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
TIMEOUT  = float(os.environ.get("TRACKER_TIMEOUT", "30"))

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT, follow_redirects=True)
    return _client


def _call(method: str, path: str, **kwargs: Any) -> Any:
    """Call the Tracker API and raise ValueError on any non-2xx response."""
    try:
        resp = _get_client().request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise ValueError(
            f"Tracker API unreachable at {BASE_URL}: {exc}. "
            f"Start the Flask app with `python app.py`."
        ) from exc

    if resp.status_code >= 400:
        try:
            payload = resp.json()
            err = payload.get("error") if isinstance(payload, dict) else payload
        except Exception:
            err = resp.text or f"HTTP {resp.status_code}"
        raise ValueError(f"Tracker API {method} {path} -> {resp.status_code}: {err}")

    ctype = resp.headers.get("content-type", "")
    return resp.json() if ctype.startswith("application/json") else resp.text


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
mcp = _MCP("tracker-mcp-server")


# ---------- health / discovery ----------
@mcp.tool()
def health_check() -> dict:
    """Verify the Tracker API is reachable. Returns base URL and basic counts."""
    try:
        meta = _call("GET", "/api/meta")
        projects = _call("GET", "/api/projects")
        return {
            "ok": True,
            "base_url": BASE_URL,
            "issue_types": len(meta["issue_types"]),
            "statuses": len(meta["statuses"]),
            "resolutions": len(meta["resolutions"]),
            "projects": len(projects),
        }
    except Exception as exc:
        return {"ok": False, "base_url": BASE_URL, "error": str(exc)}


@mcp.tool()
def get_meta() -> dict:
    """Return reference data: issue_types, statuses (with category), priorities,
    resolutions, link_types (with is_blocking flag), and active users."""
    return _call("GET", "/api/meta")


# ---------- projects ----------
@mcp.tool()
def list_projects() -> list[dict]:
    """List all tracker projects (id, project_key, name, description)."""
    return _call("GET", "/api/projects")


@mcp.tool()
def create_project(project_key: str, name: str, description: str = "") -> dict:
    """Create a project.

    Args:
        project_key: Short uppercase key used as the issue-key prefix, e.g. "MOB".
        name: Display name, e.g. "Mobile App".
        description: Optional free text.
    """
    return _call("POST", "/api/projects", json={
        "project_key": project_key.strip().upper(),
        "name": name.strip(),
        "description": description,
    })


@mcp.tool()
def get_project_board(project_id: int) -> dict:
    """Return the full project board: every deliverable with its tickets,
    subtasks, bugs, statuses, and live readiness counters."""
    return _call("GET", f"/api/projects/{project_id}/board")


@mcp.tool()
def summarize_project(project_id: int) -> dict:
    """Compact summary of a project: per-deliverable ticket progress,
    open blocking bugs, and readiness flags. Use this before accepting
    a Deliverable."""
    board = _call("GET", f"/api/projects/{project_id}/board")
    return {
        "project": board["project"],
        "deliverables": [
            {
                "issue_key": d["issue_key"],
                "summary": d["summary"],
                "status": d["status"]["name"],
                "tickets_total": d["readiness"]["tickets_total"],
                "tickets_done": d["readiness"]["tickets_done"],
                "open_blocking_bugs": d["readiness"]["open_blocking_bugs"],
                "ready_for_verification": d["readiness"]["ready_for_verification"],
                "ready_for_acceptance": d["readiness"]["ready_for_acceptance"],
            }
            for d in board["deliverables"]
        ],
    }


# ---------- deliverables ----------
@mcp.tool()
def create_deliverable(
    project_id: int,
    summary: str,
    acceptance_criteria: str,
    description: str = "",
    target_date: str | None = None,
) -> dict:
    """Create a Deliverable issue — the unit of committed outcome.

    Args:
        project_id: Numeric project id (from list_projects).
        summary: One-line summary, e.g. "Passwordless OTP login v1.0".
        acceptance_criteria: Definition of Done. REQUIRED — the Deliverable
            cannot leave Draft without it.
        description: Optional detail.
        target_date: Optional YYYY-MM-DD target date.
    """
    body: dict[str, Any] = {
        "project_id": project_id,
        "summary": summary.strip(),
        "acceptance_criteria": acceptance_criteria.strip(),
        "description": description,
    }
    if target_date:
        body["target_date"] = target_date
    return _call("POST", "/api/deliverables", json=body)


# ---------- tickets ----------
@mcp.tool()
def create_ticket(
    project_id: int,
    parent_id: int,
    summary: str,
    issue_type: str = "Story",
    description: str = "",
    priority: str = "Medium",
) -> dict:
    """Create a Ticket (Story or Task) under a Deliverable.

    Args:
        project_id: Numeric project id.
        parent_id: Issue id of the parent Deliverable (or Epic).
        summary: One-line summary.
        issue_type: "Story" or "Task". Default "Story".
        description: Optional detail.
        priority: One of Highest, High, Medium, Low, Lowest. Default "Medium".
    """
    return _call("POST", "/api/tickets", json={
        "project_id": project_id,
        "parent_id": parent_id,
        "issue_type": issue_type,
        "summary": summary.strip(),
        "description": description,
        "priority": priority,
    })


# ---------- bugs ----------
@mcp.tool()
def create_bug(
    source_ticket_id: int,
    summary: str,
    description: str = "",
    priority: str = "High",
) -> dict:
    """Create a Bug. The API auto-links the bug with 'Blocks' to BOTH the
    source ticket and the source ticket's Deliverable root (invariant A1).

    Args:
        source_ticket_id: Issue id of the ticket where the defect was found.
        summary: One-line summary of the defect.
        description: Optional repro steps.
        priority: One of Highest, High, Medium, Low, Lowest. Default "High".
    """
    return _call("POST", "/api/bugs", json={
        "source_ticket_id": source_ticket_id,
        "summary": summary.strip(),
        "description": description,
        "priority": priority,
    })


# ---------- lookup ----------
@mcp.tool()
def find_issue_by_key(project_id: int, issue_key: str) -> dict:
    """Find an issue by key (e.g. "MOB-3") within a project. Returns its
    numeric id, type, and current status. Use this before any tool that
    takes an issue_id when the user gives you a key."""
    board = _call("GET", f"/api/projects/{project_id}/board")
    target = issue_key.strip().upper()

    for d in board["deliverables"]:
        if d["issue_key"] == target:
            return {"id": d["id"], "issue_key": d["issue_key"],
                    "summary": d["summary"], "type": "Deliverable",
                    "status": d["status"]["name"]}
        for t in d["tickets"]:
            if t["issue"]["issue_key"] == target:
                return {"id": t["issue"]["id"], "issue_key": t["issue"]["issue_key"],
                        "summary": t["issue"]["summary"], "type": "Ticket",
                        "status": t["status"]["name"]}
            for s in t["subtasks"]:
                if s["issue_key"] == target:
                    return {"id": s["id"], "issue_key": s["issue_key"],
                            "summary": s["summary"], "type": "Sub-task",
                            "status": "n/a"}
        for b in d["bugs"]:
            if b["issue"]["issue_key"] == target:
                return {"id": b["issue"]["id"], "issue_key": b["issue"]["issue_key"],
                        "summary": b["issue"]["summary"], "type": "Bug",
                        "status": b["status"]["name"]}

    raise ValueError(f"Issue {issue_key} not found in project {project_id}")


@mcp.tool()
def get_issue(issue_id: int) -> dict:
    """Get full detail for an issue: type, status, links, comments, recent
    history, and — for Deliverables — live readiness."""
    return _call("GET", f"/api/issues/{issue_id}")


# ---------- workflow ----------
@mcp.tool()
def get_available_actions(issue_id: int) -> list[dict]:
    """List the workflow actions currently available on an issue (each action
    is a legal transition from the issue's current status)."""
    return _call("GET", f"/api/issues/{issue_id}/available-actions")


@mcp.tool()
def transition_issue(
    issue_id: int,
    action: str,
    resolution_id: int | None = None,
) -> dict:
    """Apply a workflow action to an issue.

    Valid actions depend on issue type:
      Deliverable: start, ready, verify, return, accept, cancel
      Ticket:      start, review, return, done, cancel
      Bug:         start, fix, reopen, verify, close, wontfix

    Args:
        issue_id: Numeric issue id.
        action: One of the actions above.
        resolution_id: REQUIRED for done / fix / close / wontfix.
            Get ids from get_meta() — usually 1 = "Fixed".
    """
    body: dict[str, Any] = {"action": action}
    if resolution_id is not None:
        body["resolution_id"] = resolution_id
    return _call("POST", f"/api/issues/{issue_id}/transition", json=body)


@mcp.tool()
def get_readiness(issue_id: int) -> dict:
    """Return the readiness of a Deliverable: ticket totals, open blocking
    bugs, and the ready_for_verification / ready_for_acceptance flags.
    Call this before attempting to accept a Deliverable."""
    detail = _call("GET", f"/api/issues/{issue_id}")
    if "readiness" not in detail:
        raise ValueError(f"Issue {issue_id} is not a Deliverable")
    return detail["readiness"]


# ---------- comments ----------
@mcp.tool()
def add_comment(issue_id: int, body: str) -> dict:
    """Add a comment to an issue."""
    return _call("POST", f"/api/issues/{issue_id}/comment", json={"body": body.strip()})


# ---------- demo ----------
@mcp.tool()
def seed_demo() -> dict:
    """Seed the MOB project with the complete DLV-1 demo: one Deliverable,
    four Tickets, two Bugs, full bug lifecycle, and Deliverable acceptance.
    Use only when the user explicitly asks to seed demo data."""
    return _call("POST", "/api/demo/seed", json={})


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")