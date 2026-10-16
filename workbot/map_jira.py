"""Resolve and cache each user's Jira accountId from their Slack email.

Work emails rarely change, so this is a maintenance command you run on demand
(e.g. after someone joins), not a live lookup:

    docker compose exec workbot python -m workbot.map_jira [--refresh] [USER_ID ...]

With no arguments it maps every user the bot already knows; pass explicit Slack
user IDs to (pre-)map specific people. --refresh re-resolves users that already
have an accountId.

Needs the Slack bot scope `users:read.email` (to read member emails) plus the
JIRA_* env the bot already uses. The resolved jira_account_id is cached on the
user row; the assignment-detection poll reads that, never the email - so this
scope is only exercised here, at (re)generation time.
"""

import argparse
import logging
import os
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import db, jira

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("workbot.map_jira")


def _slack_email(client, user_id):
    """(email, None) or (None, reason) for one Slack user."""
    try:
        info = client.users_info(user=user_id)["user"]
    except SlackApiError as e:
        return None, f"slack error: {e.response.get('error')}"
    if info.get("is_bot") or info.get("deleted"):
        return None, "bot/deleted"
    email = (info.get("profile") or {}).get("email")
    if not email:
        return None, "no email (scope users:read.email?)"
    return email, None


def map_users(user_ids=None, refresh=False):
    if not jira.configured():
        log.error("Jira is not configured (JIRA_* env); aborting.")
        return 1
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log.error("SLACK_BOT_TOKEN missing; aborting.")
        return 1
    client = WebClient(token=token)
    db.init_db()

    if user_ids:
        # Allow pre-mapping someone who hasn't used the bot yet.
        for uid in user_ids:
            db.ensure_user(uid)
        rows = [db.get_user(uid) for uid in user_ids]
    else:
        rows = db.all_users()

    mapped = skipped = no_email = no_match = 0
    no_email_ids = []
    for u in rows:
        uid = u["user_id"]
        if u.get("jira_account_id") and not refresh:
            skipped += 1
            continue
        email, reason = _slack_email(client, uid)
        if not email:
            no_email += 1
            no_email_ids.append(f"{uid} ({reason})")
            continue
        res = jira.search_user_by_email(email)
        if not res:
            no_match += 1
            log.info("  no Jira match: %s <%s>", uid, email)
            continue
        account_id, matched = res
        db.set_jira_identity(uid, account_id, email)
        mapped += 1
        log.info("  mapped %s <%s> -> %s", uid, email, account_id)

    log.info(
        "Done: %d mapped, %d already-mapped, %d no-email, %d no-Jira-match",
        mapped, skipped, no_email, no_match,
    )
    if no_email_ids:
        log.info("No-email users (often the missing scope, or bots): %s", ", ".join(no_email_ids))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Cache Jira accountIds from Slack emails.")
    p.add_argument("user_ids", nargs="*",
                   help="Slack user IDs to map (default: all users the bot knows)")
    p.add_argument("--refresh", action="store_true",
                   help="Re-resolve users that already have an accountId")
    args = p.parse_args(argv)
    return map_users(args.user_ids or None, args.refresh)


if __name__ == "__main__":
    sys.exit(main())
