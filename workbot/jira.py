"""Minimal Jira client - just enough to populate the project multi-select.

Uses Basic auth (email + API token) against the Cloud REST API. All config is
read from the environment; if any piece is missing the client is simply
"not configured" and callers skip the Jira features.
"""

import base64
import json
import logging
import os
import urllib.request

logger = logging.getLogger("workbot")

BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("JIRA_EMAIL", "")
TOKEN = os.environ.get("JIRA_TOKEN", "")


def configured():
    """True only when we have everything needed to call Jira."""
    return bool(BASE_URL and EMAIL and TOKEN)


def _auth_header():
    # Jira Cloud wants Basic base64(email:api_token).
    raw = f"{EMAIL}:{TOKEN}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# Unassigned, actionable, non-trivial work across all projects (filtered per
# user by project later). Priority-ordered so a truncated fetch keeps the top.
CANDIDATE_JQL = (
    'assignee IS EMPTY AND statusCategory = "To Do" '
    "AND issuetype NOT IN (Epic, Sub-task) "
    # Low/Lowest are included but sorted to the bottom by priority DESC, so they
    # only surface as a last resort when nothing better fits the role.
    "ORDER BY priority DESC, created ASC"
)


def _post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE_URL + path, data=data, method="POST",
        headers={"Authorization": _auth_header(), "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_candidates(limit=500):
    """All unassigned To-Do tasks (key/summary/priority/project), priority-first.

    Fetched hourly and cached; callers filter it per user by project. Returns []
    on failure so a hiccup just means "no suggestions this round".
    """
    if not configured():
        return []
    out = []
    token = None
    try:
        # Enhanced JQL search paginates by nextPageToken.
        while len(out) < limit:
            body = {"jql": CANDIDATE_JQL, "fields": ["key", "summary", "priority", "project"],
                    "maxResults": min(100, limit - len(out))}
            if token:
                body["nextPageToken"] = token
            data = _post("/rest/api/3/search/jql", body)
            for i in data.get("issues", []):
                f = i.get("fields", {})
                out.append({
                    "key": i["key"],
                    "summary": f.get("summary", ""),
                    "priority": (f.get("priority") or {}).get("name", ""),
                    "project": (f.get("project") or {}).get("key", ""),
                })
            token = data.get("nextPageToken")
            if not token:
                break
        return out[:limit]
    except Exception:
        logger.exception("Jira candidate fetch failed")
        return []


def fetch_projects():
    """Return [{'key':..., 'name':...}] for every visible project, name-sorted.

    Returns [] on any failure so the dropdown degrades to "not shown" rather
    than breaking the Settings modal.
    """
    if not configured():
        return []
    projects = []
    start = 0
    try:
        # project/search is paginated; walk until isLast.
        while True:
            url = f"{BASE_URL}/rest/api/3/project/search?maxResults=50&startAt={start}&orderBy=name"
            req = urllib.request.Request(
                url, headers={"Authorization": _auth_header(), "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            for p in data.get("values", []):
                projects.append({"key": p["key"], "name": p["name"]})
            if data.get("isLast", True) or not data.get("values"):
                break
            start += len(data["values"])
        return projects
    except Exception:
        logger.exception("Jira project fetch failed")
        return []
