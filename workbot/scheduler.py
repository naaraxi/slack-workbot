"""The proactive side of WorkBot.

Two background jobs:
  - check-in tick (every 60s): DM each eligible user the Yes/No/Almost Done
    question when their interval is due, during their working hours.
  - report flush (every WORKBOT_REPORT_FLUSH_SECONDS): post one combined
    "needs a task" message for everyone who answered No since the last flush.

Silent by default: the tick only ever iterates db.enabled_users(), i.e. users
who ran /workbot, enabled it, and set working hours. It also skips users who
are out of office (status emoji), snoozed (Almost Done), outside working
hours, or paused after ignoring several check-ins in a row.
"""

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, messages

logger = logging.getLogger("workbot")

# user_ids who answered No and are waiting to be reported, guarded by a lock
_pending = set()
_pending_lock = threading.Lock()

PLACEHOLDER_CHANNEL = "C0000000000"

# Consecutive ignored check-ins before the bot pauses a user for the day.
IGNORE_THRESHOLD = int(os.environ.get("WORKBOT_IGNORE_THRESHOLD", "3"))

# Delete the bot's own DM messages older than this many hours (0 = never).
PRUNE_AFTER_HOURS = int(os.environ.get("WORKBOT_PRUNE_AFTER_HOURS", "24"))

# Emoji reactions the bot pre-seeds on each report (comma-separated names).
REPORT_REACTIONS = [
    e.strip()
    for e in os.environ.get("WORKBOT_REPORT_REACTIONS", "white_check_mark,eyes").split(",")
    if e.strip()
]

# Status emojis that mean "leave me alone" (out of office). Configurable.
_DEFAULT_SKIP_EMOJIS = ":palm_tree:,:face_with_thermometer:"
SKIP_EMOJIS = {
    e.strip()
    for e in os.environ.get("WORKBOT_SKIP_STATUS_EMOJIS", _DEFAULT_SKIP_EMOJIS).split(",")
    if e.strip()
}


def add_pending(user_id):
    with _pending_lock:
        _pending.add(user_id)


def resolve_report_mention(client, handle):
    """Resolve a user-group handle (e.g. 'projectmanagers') to a Slack subteam
    mention token '<!subteam^ID>', caching the ID in the DB.

    The ID is re-resolved only when the handle changes, so once resolved it
    keeps working even if the usergroups:read scope is later removed. Returns
    "" (no prefix) if the handle is empty or cannot be resolved.
    """
    if not handle:
        return ""
    handle = handle.lstrip("@").strip()

    cached_handle = db.meta_get("report_group_handle")
    cached_id = db.meta_get("report_subteam_id")
    if cached_handle == handle and cached_id:
        return f"<!subteam^{cached_id}>"

    try:
        for g in client.usergroups_list().get("usergroups", []):
            if g.get("handle") == handle:
                db.meta_set("report_group_handle", handle)
                db.meta_set("report_subteam_id", g["id"])
                logger.info("Resolved report group @%s -> %s", handle, g["id"])
                return f"<!subteam^{g['id']}>"
        logger.warning("Report group @%s not found via usergroups.list", handle)
    except Exception:
        logger.exception(
            "Could not resolve report group @%s (needs the usergroups:read "
            "bot scope?)", handle
        )
    return ""


def is_unavailable(client, user_id):
    """True if the user's Slack status marks them out of office.

    Fails closed: if the status can't be read, treat the user as unavailable
    so the bot stays quiet rather than risk pinging someone who is out.
    """
    try:
        profile = client.users_info(user=user_id)["user"]["profile"]
        return (profile.get("status_emoji") or "") in SKIP_EMOJIS
    except Exception:
        logger.exception("Could not read status for %s; treating as unavailable", user_id)
        return True


def _resolve_channel_id(client, value):
    """A channel ID as-is, or resolve a public-channel #name to its ID."""
    v = value.strip()
    if re.fullmatch(r"[CG][A-Z0-9]{6,}", v):
        return v
    name = v.lstrip("#")
    try:
        cursor = None
        while True:
            resp = client.conversations_list(
                types="public_channel", limit=1000, cursor=cursor, exclude_archived=True
            )
            for ch in resp.get("channels", []):
                if ch.get("name") == name:
                    return ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        logger.warning("Report channel '%s' not found to join", value)
    except Exception:
        logger.exception("Could not resolve channel '%s' to join (channels:read?)", value)
    return None


def ensure_in_channel(client, report_channel):
    """Join the report channel at startup (public channels only; idempotent).

    Posts the one-time intro message only when the bot *actually* joins a
    channel it has never introduced itself to (tracked in the DB), so a restart
    (already a member) or a leave-and-rejoin never reposts it.
    """
    if not report_channel or report_channel == PLACEHOLDER_CHANNEL:
        return
    cid = _resolve_channel_id(client, report_channel)
    if not cid:
        return
    try:
        resp = client.conversations_join(channel=cid)
    except Exception:
        logger.exception("Could not join report channel %s (channels:join?)", cid)
        return

    warnings = (resp.get("response_metadata") or {}).get("warnings") or []
    newly_joined = resp.get("warning") != "already_in_channel" and "already_in_channel" not in warnings
    if not newly_joined:
        return  # restart / already a member
    if db.meta_get(f"intro:{cid}"):
        return  # introduced here before; never repeat

    try:
        client.chat_postMessage(channel=cid, text=messages.join_message())
        db.meta_set(f"intro:{cid}", _now_utc().isoformat())
        logger.info("Joined %s and posted intro", cid)
    except Exception:
        logger.exception("Could not post join message to %s", cid)


def _now_utc():
    return datetime.now(timezone.utc)


def work_days(user):
    """Set of ISO weekdays (Mon=1..Sun=7) the user wants check-ins; default Mon-Fri."""
    raw = user.get("work_days") or "1,2,3,4,5"
    days = {int(x) for x in str(raw).split(",") if x.strip().isdigit()}
    return days or {1, 2, 3, 4, 5}


def within_hours(user):
    tz = user.get("tz") or "UTC"
    try:
        now_local = _now_utc().astimezone(ZoneInfo(tz))
    except Exception:
        logger.exception("Bad tz %r for %s; treating as always in-hours", tz, user["user_id"])
        return True
    if now_local.isoweekday() not in work_days(user):
        return False  # not a working day for this user
    mins = now_local.hour * 60 + now_local.minute
    return user["work_start_min"] <= mins < user["work_end_min"]


def _local_day(user):
    """Current date in the user's timezone, as 'YYYY-MM-DD'."""
    tz = user.get("tz") or "UTC"
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    return _now_utc().astimezone(zone).date().isoformat()


def _end_of_workday_utc(user):
    """ISO-8601 UTC of today's work_end in the user's timezone."""
    tz = user.get("tz") or "UTC"
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    now_local = _now_utc().astimezone(zone)
    end_local = now_local.replace(
        hour=user["work_end_min"] // 60,
        minute=user["work_end_min"] % 60,
        second=0,
        microsecond=0,
    )
    return end_local.astimezone(timezone.utc).isoformat()


def _next_working_start(now_local, user, zone):
    """Start-of-work on the next working day after today, as aware UTC."""
    days = work_days(user)
    for i in range(1, 8):
        d = now_local + timedelta(days=i)
        if d.isoweekday() in days:
            s = d.replace(
                hour=user["work_start_min"] // 60, minute=user["work_start_min"] % 60,
                second=0, microsecond=0,
            )
            return s.astimezone(timezone.utc)
    return None


def next_checkin_dt(user):
    """Best estimate of the next check-in time as an aware UTC datetime, used
    only for display on the Home tab. Returns None if not determinable."""
    tz = user.get("tz") or "UTC"
    try:
        zone = ZoneInfo(tz)
    except Exception:
        return None
    if user.get("work_start_min") is None or user.get("work_end_min") is None:
        return None

    days = work_days(user)
    now_local = _now_utc().astimezone(zone)
    start = now_local.replace(
        hour=user["work_start_min"] // 60, minute=user["work_start_min"] % 60,
        second=0, microsecond=0,
    )
    end = now_local.replace(
        hour=user["work_end_min"] // 60, minute=user["work_end_min"] % 60,
        second=0, microsecond=0,
    )
    today_working = now_local.isoweekday() in days

    if today_working and now_local < start:
        return start.astimezone(timezone.utc)

    if today_working and now_local < end:
        # Within hours: last check-in + interval, clamped to now.
        interval = timedelta(minutes=user.get("interval_minutes") or 60)
        la = user.get("last_asked")
        base = None
        if la:
            try:
                base = datetime.fromisoformat(la) + interval
            except ValueError:
                base = None
        if base is None or base < _now_utc():
            base = _now_utc()
        base_local = base.astimezone(zone)
        if base_local < end:
            return base_local.astimezone(timezone.utc)

    # After hours, or not a working day: next working day's start.
    return _next_working_start(now_local, user, zone)


def _snoozed(user):
    su = user.get("snooze_until")
    if not su:
        return False
    try:
        return datetime.fromisoformat(su) > _now_utc()
    except ValueError:
        return False


def _paused(user):
    pu = user.get("paused_until")
    if not pu:
        return False
    try:
        return datetime.fromisoformat(pu) > _now_utc()
    except ValueError:
        return False


def _due(user):
    la = user.get("last_asked")
    if not la:
        return True
    try:
        last = datetime.fromisoformat(la)
    except ValueError:
        return True
    return _now_utc() - last >= timedelta(minutes=user["interval_minutes"])


def _dm(client, user_id, text, blocks=None):
    channel = client.conversations_open(users=user_id)["channel"]["id"]
    client.chat_postMessage(channel=channel, text=text, blocks=blocks)


def supersede(client, user):
    """Edit an outstanding check-in to an 'expired' note, dropping its buttons."""
    ch = user.get("last_msg_channel")
    ts = user.get("last_msg_ts")
    if not (ch and ts and user.get("awaiting_reply")):
        return
    lang = user.get("lang")
    try:
        client.chat_update(
            channel=ch,
            ts=ts,
            text=messages.expired(lang),
            blocks=messages.confirm_blocks(messages.expired(lang)),
        )
    except Exception:
        logger.exception("Failed to supersede check-in for %s", user.get("user_id"))


def send_checkin(client, user):
    """DM the check-in question to one user and record it as outstanding."""
    lang = user.get("lang")
    channel = client.conversations_open(users=user["user_id"])["channel"]["id"]
    resp = client.chat_postMessage(
        channel=channel,
        blocks=messages.checkin_blocks(lang),
        text=messages.checkin_fallback(lang),
    )
    db.record_sent(user["user_id"], channel, resp["ts"])


def _process_user(client, user):
    uid = user["user_id"]

    # Daily reset at local midnight: clear yesterday's per-day state so a "No"
    # (or ignores, pause, snooze) never carries into the new morning.
    today = _local_day(user)
    if user.get("last_reset_day") != today:
        if user.get("awaiting_reply"):
            supersede(client, user)
            db.record_event(uid, "ignored")  # last check-in of the day went unanswered
        db.daily_reset(uid, today)
        user = db.get_user(uid)

    # Lift an expired pause before anything else.
    if user.get("paused_until"):
        if _paused(user):
            return
        db.resume(uid)
        user = db.get_user(uid)

    if _snoozed(user) or not within_hours(user) or not _due(user):
        return
    if is_unavailable(client, uid):
        return

    if user.get("awaiting_reply"):
        # The previous check-in was never answered: count it as ignored.
        count = (user.get("consecutive_ignored") or 0) + 1
        supersede(client, user)
        db.record_event(uid, "ignored")  # log for stats / export
        if count >= IGNORE_THRESHOLD:
            db.set_paused(uid, _end_of_workday_utc(user))
            lang = user.get("lang")
            _dm(client, uid, messages.paused(lang), messages.paused_blocks(lang))
            return
        db.bump_ignored(uid, count)

    send_checkin(client, user)


def _tick(client):
    for user in db.enabled_users():
        try:
            _process_user(client, user)
        except Exception:
            logger.exception("check-in failed for %s", user.get("user_id"))


def _flush_reports(client, report_channel, report_mention=""):
    with _pending_lock:
        if not _pending:
            return
        ids = list(_pending)
        _pending.clear()

    # Drop anyone who went out of office between answering and this flush.
    ids = [u for u in ids if not is_unavailable(client, u)]
    if not ids:
        return

    text = messages.needs_task_text(ids, mention=report_mention)
    if not report_channel or report_channel == PLACEHOLDER_CHANNEL:
        logger.warning("No report channel configured; would have posted: %s", text)
        return
    try:
        resp = client.chat_postMessage(channel=report_channel, text=text)
    except Exception:
        logger.exception("Failed to post report; re-queuing %s", ids)
        with _pending_lock:
            _pending.update(ids)
        return
    # Pre-seed reactions so PMs can one-click to acknowledge / claim the task.
    _add_reactions(client, resp["channel"], resp["ts"])


def _add_reactions(client, channel, ts):
    """Add the configured acknowledgement reactions to a report message."""
    for emoji in REPORT_REACTIONS:
        try:
            client.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except Exception:
            # A missing scope or an already-added reaction shouldn't break reports.
            logger.exception("Could not add reaction :%s: to report", emoji)


def _prune_dms(client):
    """Delete the bot's own DM messages older than PRUNE_AFTER_HOURS, for every
    enabled user. Only messages WorkBot posted (bot_id present) are removed, and
    only in direct messages; the user's own messages are left untouched."""
    if PRUNE_AFTER_HOURS <= 0:
        return
    cutoff = (_now_utc() - timedelta(hours=PRUNE_AFTER_HOURS)).timestamp()
    latest = f"{cutoff:.6f}"
    for user in db.enabled_users():
        uid = user["user_id"]
        try:
            channel = client.conversations_open(users=uid)["channel"]["id"]
        except Exception:
            logger.exception("prune: could not open DM for %s", uid)
            continue
        deleted = 0
        cursor = None
        try:
            while True:
                resp = client.conversations_history(
                    channel=channel, latest=latest, limit=200, cursor=cursor
                )
                for msg in resp.get("messages", []):
                    if not msg.get("bot_id"):
                        continue  # leave the user's own messages
                    try:
                        client.chat_delete(channel=channel, ts=msg["ts"])
                        deleted += 1
                    except Exception:
                        pass  # already gone / not deletable
                if not resp.get("has_more"):
                    break
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            if deleted:
                logger.info("Pruned %d old DM message(s) for %s", deleted, uid)
        except Exception:
            logger.exception("prune failed for %s", uid)


def start(client, report_channel, flush_seconds=60, report_mention=""):
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(lambda: _tick(client), "interval", seconds=60, id="checkin")
    sched.add_job(
        lambda: _flush_reports(client, report_channel, report_mention),
        "interval",
        seconds=flush_seconds,
        id="flush",
    )
    if PRUNE_AFTER_HOURS > 0:
        sched.add_job(lambda: _prune_dms(client), "interval", seconds=3600, id="prune")
    sched.start()
    logger.info(
        "Scheduler started (check-in 60s, report flush %ss, ignore threshold %s, "
        "prune DMs after %sh, channel=%s)",
        flush_seconds,
        IGNORE_THRESHOLD,
        PRUNE_AFTER_HOURS or "off",
        report_channel or "unset",
    )
    return sched
