"""Assignment watch - closes the loop on a "No".

When a user answered No, the bot reported it and suggested tasks in a thread.
This polls Jira for each such user's most recent open thread and, once they get
assigned a task (after that No), replies in that thread: "@user was assigned
KEY - summary (Status), by @assigner". Then the thread is resolved and dropped
from the watch.

Only the user's latest open thread is watched - a newer No supersedes older ones
(handled in db.record_report_thread), so the announcement always lands on the
most recent notification. Pure Jira + Slack; no Codex.
"""

import logging
import os
import threading
from datetime import datetime, timezone

from . import db, jira, messages

logger = logging.getLogger("workbot")

# Default off: opt in once the identity mapping (map_jira) has been run.
ENABLED = os.environ.get("WORKBOT_ASSIGN_WATCH_ENABLED", "0") not in ("0", "", "false", "no")
POLL_SECONDS = int(os.environ.get("WORKBOT_ASSIGN_POLL_SECONDS", "300"))
SUMMARY_MAX = int(os.environ.get("WORKBOT_ASSIGN_SUMMARY_MAX", "80"))


def enabled():
    return ENABLED and jira.configured()


def _actor(change):
    """Who assigned it, ready to render: a Slack mention when the assigner maps to
    a known Slack user, else their Jira display name, else None."""
    uid = db.slack_for_jira_account(change.get("by_account"))
    if uid:
        return f"<@{uid}>"
    return change.get("by_name") or None


def _item(match):
    """Turn a Jira match into the display dict messages.assigned_text wants."""
    summary = (match.get("summary") or "").strip()
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX - 1].rstrip() + "…"
    return {
        "key": match["key"],
        "url": f"{jira.BASE_URL}/browse/{match['key']}",
        "summary": summary,
        "status": match.get("status"),
        "by": _actor(match),
    }


def _announce(client, row, matches):
    items = [_item(m) for m in matches]
    text = messages.assigned_text(row["user_id"], items)
    try:
        client.chat_postMessage(channel=row["channel"], thread_ts=row["thread_ts"],
                                text=text, unfurl_links=False, unfurl_media=False)
    except Exception:
        logger.exception("Could not post assignment notice for %s", row["user_id"])
        return
    db.resolve_report_thread(row["channel"], row["thread_ts"])
    logger.info("Announced %d assignment(s) for %s: %s",
                len(items), row["user_id"], ", ".join(i["key"] for i in items))


def _today_start_iso():
    """Start of the current UTC day - the watch only considers today's No threads,
    so a resolved recent thread never makes it walk back onto older ones."""
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()


def _watch(client):
    since = _today_start_iso()
    db.resolve_old_threads(since)  # retire pre-today open threads once and for all
    for row in db.watched_report_threads(since):
        user = db.get_user(row["user_id"])
        account_id = user and user.get("jira_account_id")
        if not account_id:
            continue  # unmapped user - nothing to query
        try:
            matches = jira.assignments_since(account_id, row["created_at"])
        except Exception:
            logger.exception("Assignment check failed for %s", row["user_id"])
            continue
        if matches:
            _announce(client, row, matches)


def watch(client):
    """One poll pass over every watched thread (scheduled). No-ops unless enabled."""
    if not enabled():
        return
    _watch(client)


def spawn_watch(client):
    """Run a poll pass in the background, so a slow Jira round never blocks the
    scheduler thread."""
    if not enabled():
        return
    threading.Thread(target=_watch, args=(client,), daemon=True).start()
