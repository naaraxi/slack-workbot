"""Minimal Jira client - just enough to populate the project multi-select.

Uses Basic auth (email + API token) against the Cloud REST API. All config is
read from the environment; if any piece is missing the client is simply
"not configured" and callers skip the Jira features.
"""

import base64
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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


def _csv_env(var):
    """A comma-separated env var as a clean list of values."""
    return [s.strip() for s in os.environ.get(var, "").split(",") if s.strip()]


def _jql_name_list(names):
    """Quote a list of status names for a JQL IN/NOT IN clause."""
    return ", ".join('"' + n.replace('"', '\\"') + '"' for n in names)


def _status_clause():
    """Which statuses count as candidate-eligible, configurable from the env:

    - WORKBOT_CANDIDATE_STATUSES: allowlist -> `status IN (...)`.
    - WORKBOT_CANDIDATE_EXCLUDE_STATUSES: denylist, always subtracted.
    - neither set -> `statusCategory = "To Do"`, the robust default that works
      across projects without naming each project's own status strings.
    """
    include = _csv_env("WORKBOT_CANDIDATE_STATUSES")
    exclude = _csv_env("WORKBOT_CANDIDATE_EXCLUDE_STATUSES")
    base = f"status IN ({_jql_name_list(include)})" if include else 'statusCategory = "To Do"'
    return f"{base} AND status NOT IN ({_jql_name_list(exclude)})" if exclude else base


def candidate_jql():
    """Unassigned, actionable, non-trivial work across all projects (filtered per
    user by project later). Priority-ordered so a truncated fetch keeps the top.
    Low/Lowest stay in but sort to the bottom, surfacing only as a last resort."""
    return (
        f"assignee IS EMPTY AND {_status_clause()} "
        "AND issuetype NOT IN (Epic, Sub-task) "
        "ORDER BY priority DESC, created ASC"
    )


def _get(path):
    req = urllib.request.Request(
        BASE_URL + path, method="GET",
        headers={"Authorization": _auth_header(), "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def search_user_by_email(email):
    """Resolve a work email to a Jira accountId via user search, or None. Prefers
    an exact, active atlassian account; when Jira hides emails (privacy settings)
    and returns a single match, trusts that. Returns (account_id, matched_email)."""
    if not (configured() and email):
        return None
    import urllib.parse
    try:
        results = _get("/rest/api/3/user/search?query=" + urllib.parse.quote(email))
    except Exception:
        logger.exception("Jira user search failed for %s", email)
        return None
    if not results:
        return None
    target = email.strip().lower()
    # Prefer an exact email match on a real, active user.
    for u in results:
        if (u.get("emailAddress") or "").strip().lower() == target \
           and u.get("active") and u.get("accountType") == "atlassian":
            return (u["accountId"], u.get("emailAddress"))
    # Email hidden or no exact hit: accept a lone atlassian result (the query
    # already matched server-side on the email).
    atlassian = [u for u in results if u.get("accountType") == "atlassian"]
    if len(atlassian) == 1:
        u = atlassian[0]
        return (u["accountId"], u.get("emailAddress"))
    return None  # ambiguous - don't guess


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
            body = {"jql": candidate_jql(), "fields": ["key", "summary", "priority", "project"],
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


def _dt(s):
    """Parse an ISO-8601 timestamp (ours, or Jira's '+0000' form) to an aware
    datetime, or None. Python 3.11+ fromisoformat handles both."""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _assignee_change_after(issue_key, account_id, after_iso):
    """The most recent change that set this issue's assignee to account_id, if it
    happened strictly after after_iso. Returns {created, by_account, by_name,
    from_name} or None. The exact time check lives here (not in JQL) to dodge
    JQL's timezone-sensitive date parsing."""
    after = _dt(after_iso)
    try:
        data = _get(f"/rest/api/3/issue/{urllib.parse.quote(issue_key)}?expand=changelog&fields=summary")
    except Exception:
        logger.exception("changelog fetch failed for %s", issue_key)
        return None
    latest = None
    for h in data.get("changelog", {}).get("histories", []):
        created = _dt(h.get("created", ""))
        if not created or (after and created <= after):
            continue
        for it in h.get("items", []):
            if it.get("field") == "assignee" and it.get("to") == account_id:
                if latest is None or created > latest["_created_dt"]:
                    author = h.get("author", {})
                    latest = {
                        "_created_dt": created,
                        "created": h.get("created"),
                        "by_account": author.get("accountId"),
                        "by_name": author.get("displayName"),
                        "from_name": it.get("fromString"),
                    }
    return latest


def assignments_since(account_id, after_iso):
    """Every task assigned to account_id after after_iso and still assigned to
    them now, most-recent first, each enriched with who assigned it. Returns a
    list of dicts (key/summary/status/created/by_account/by_name/from_name); []
    if none.

    JQL gives a coarse day-level shortlist of 'currently theirs, reassigned to
    them recently'; the exact post-No check is done per issue against the
    changelog."""
    if not (configured() and account_id):
        return []
    # Coarse JQL bound, a day BEFORE the No: JQL's date-only AFTER skips same-day
    # changes, which would miss assignments made the same day as the No. The exact
    # post-No cutoff is enforced per issue against the changelog, so the margin is
    # safe (it just hands a few extra candidates to _assignee_change_after).
    after_dt = _dt(after_iso)
    day = ((after_dt - timedelta(days=1)).strftime("%Y-%m-%d")
           if after_dt else (after_iso or "")[:10])
    acc = account_id.replace('"', '')
    jql = (f'assignee = "{acc}" AND assignee CHANGED TO "{acc}" '
           f'AFTER "{day}" ORDER BY updated DESC')
    try:
        data = _post("/rest/api/3/search/jql",
                     {"jql": jql, "fields": ["key", "summary", "status"], "maxResults": 50})
    except Exception:
        logger.exception("assignment search failed for %s", account_id)
        return []
    out = []
    for issue in data.get("issues", []):
        change = _assignee_change_after(issue["key"], account_id, after_iso)
        if not change:
            continue
        f = issue.get("fields", {})
        out.append({
            "key": issue["key"],
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            **change,
        })
    out.sort(key=lambda c: c["_created_dt"], reverse=True)  # newest first
    for c in out:
        c.pop("_created_dt", None)  # internal sort key
    return out
