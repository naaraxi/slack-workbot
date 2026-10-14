"""Slack interaction handlers: /workbot, enable/disable, working-hours modal,
the Yes/No/Almost Done check-in answers, and the "free" DM.
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta

from . import db, messages, scheduler
from .scheduler import _now_utc, within_hours
from .util import hhmm_to_minutes

logger = logging.getLogger("workbot")

DEFAULT_INTERVAL = int(os.environ.get("WORKBOT_DEFAULT_INTERVAL_MINUTES", "60"))
# Minimum seconds between two accepted "free" DMs from the same user.
FREE_COOLDOWN = int(os.environ.get("WORKBOT_FREE_COOLDOWN_SECONDS", "120"))


# --- helpers --------------------------------------------------------------


def _post_response_url(response_url, payload):
    if not response_url:
        return
    try:
        req = urllib.request.Request(
            response_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        logger.exception("response_url POST failed")


def _delete_card(response_url):
    """Remove an ephemeral message previously posted to this response_url."""
    _post_response_url(response_url, {"delete_original": True})


def _update_message(client, body, text, blocks=None):
    """Replace the message a button was clicked on, dropping its buttons.

    Uses chat.update (via the message's channel+ts) rather than response_url so
    it still works on check-ins older than 30 minutes.
    """
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=text,
            blocks=blocks if blocks is not None else messages.confirm_blocks(text),
        )
    except Exception:
        logger.exception("chat_update failed")


def _home_status(client, user):
    """Live status for the Home tab: away / paused / snoozed / in-hours + the
    next expected check-in time."""
    st = {"away": False, "paused_until": None, "snooze_until": None, "next_ts": None,
          "in_hours": False, "active_users": db.active_count(),
          "stats": db.event_counts(user["user_id"])}
    if not user.get("enabled") or not user.get("configured"):
        return st
    st["in_hours"] = scheduler.within_hours(user)
    if scheduler.is_unavailable(client, user["user_id"]):
        st["away"] = True
        return st
    if scheduler._paused(user):
        st["paused_until"] = user.get("paused_until")
        return st
    if scheduler._snoozed(user):
        st["snooze_until"] = user.get("snooze_until")
        return st
    dt = scheduler.next_checkin_dt(user)
    if dt:
        st["next_ts"] = int(dt.timestamp())
    return st


def _publish_home(client, user_id):
    """Render the App Home tab for one user from current state."""
    try:
        user = db.get_user(user_id) or db.ensure_user(user_id, default_interval=DEFAULT_INTERVAL)
        st = _home_status(client, user)
        client.views_publish(user_id=user_id, view=messages.home_view(user, st))
    except Exception:
        logger.exception("views_publish failed for %s", user_id)


def _trigger_checkin_now(client, user_id):
    """Shared 'check me in now' action (Home button, contextual buttons, and the
    legacy `free` DM). Honours the cooldown; resumes any pause/snooze; sends a
    check-in if in working hours, else an off-hours note. Returns True if acted.
    """
    user = db.get_user(user_id)
    if not user or not user["enabled"]:
        return False
    now = _now_utc()
    last_free = user.get("last_free")
    if last_free:
        try:
            if (now - datetime.fromisoformat(last_free)).total_seconds() < FREE_COOLDOWN:
                return False
        except ValueError:
            pass
    db.set_last_free(user_id, now.isoformat())

    scheduler.supersede(client, user)
    db.clear_snooze(user_id)
    db.resume(user_id)
    fresh = db.get_user(user_id)
    if within_hours(fresh):
        scheduler.send_checkin(client, fresh)
    else:
        _dm(client, user_id, messages.free_ack_off_hours(fresh.get("lang")))
    return True


def _dm(client, user_id, text):
    channel = client.conversations_open(users=user_id)["channel"]["id"]
    client.chat_postMessage(channel=channel, text=text)


def _user_tz_lang(client, user_id):
    """Return (timezone, default-language) from the user's Slack profile."""
    try:
        info = client.users_info(user=user_id, include_locale=True)["user"]
        tz = info.get("tz") or "UTC"
        lang = messages.normalize_lang(info.get("locale"))
        return tz, lang
    except Exception:
        logger.exception("users_info failed for %s; using UTC/default", user_id)
        return "UTC", messages.DEFAULT_LANG


def _open_settings(client, body, user):
    """Open the settings modal, pre-filled, carrying the card's response_url."""
    _, loc_lang = _user_tz_lang(client, user["user_id"])
    client.views_open(
        trigger_id=body["trigger_id"],
        view=messages.settings_modal(
            user,
            private_metadata=body.get("response_url", ""),
            lang=user.get("lang") or loc_lang,
        ),
    )


# --- registration ---------------------------------------------------------


def register(app):
    @app.command("/workbot")
    def workbot_command(ack, body, respond):
        ack()
        user_id = body["user_id"]
        db.ensure_user(user_id, default_interval=DEFAULT_INTERVAL)
        user = db.get_user(user_id)
        respond(
            response_type="ephemeral",
            blocks=messages.status_blocks(user),
            text=messages.status_fallback(user),
        )

    @app.action("wb_enable")
    def on_enable(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.ensure_user(user_id, default_interval=DEFAULT_INTERVAL)
        if not user["configured"]:
            _open_settings(client, body, user)  # first run: collect settings
            return
        db.set_enabled(user_id, True)
        db.resume(user_id)  # clear any stale pause/ignore state
        _delete_card(body.get("response_url"))
        _dm(client, user_id, messages.enabled_confirm(db.get_user(user_id)))
        _publish_home(client, user_id)

    @app.action("wb_disable")
    def on_disable(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.ensure_user(user_id, default_interval=DEFAULT_INTERVAL)
        db.set_enabled(user_id, False)
        _delete_card(body.get("response_url"))
        _dm(client, user_id, messages.disabled_confirm(user.get("lang")))
        _publish_home(client, user_id)

    @app.action("wb_settings")
    def on_settings(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.ensure_user(user_id, default_interval=DEFAULT_INTERVAL)
        _open_settings(client, body, user)

    @app.view("wb_settings_modal")
    def on_settings_submit(ack, body, view, client):
        user_id = body["user"]["id"]
        values = view["state"]["values"]
        start = values["start_block"]["start"]["selected_time"]
        end = values["end_block"]["end"]["selected_time"]
        interval = values["interval_block"]["interval"]["selected_option"]["value"]
        lang = values["lang_block"]["lang"]["selected_option"]["value"]
        day_opts = values["days_block"]["days"].get("selected_options") or []
        days = sorted(o["value"] for o in day_opts)
        start_min = hhmm_to_minutes(start)
        end_min = hhmm_to_minutes(end)

        errors = {}
        if end_min <= start_min:
            errors["end_block"] = messages.err_end_before_start(lang)
        if not days:
            errors["days_block"] = messages.err_no_days(lang)
        if errors:
            ack(response_action="errors", errors=errors)
            return

        ack()
        user = db.ensure_user(user_id, default_interval=DEFAULT_INTERVAL)
        first_setup = not user["configured"]
        tz, _ = _user_tz_lang(client, user_id)
        db.set_working_hours(user_id, tz, start_min, end_min)
        db.set_interval(user_id, int(interval))
        db.set_lang(user_id, lang)
        db.set_work_days(user_id, ",".join(days))
        if first_setup:
            db.set_enabled(user_id, True)

        _delete_card(view.get("private_metadata"))
        fresh = db.get_user(user_id)
        if first_setup:
            _dm(client, user_id, messages.enabled_confirm(fresh))
        else:
            _dm(client, user_id, messages.settings_saved(fresh))
        _publish_home(client, user_id)

    # --- check-in answers -------------------------------------------------

    @app.action("wb_yes")
    def on_yes(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.get_user(user_id) or {}
        db.record_answer(user_id, "yes")
        db.record_event(user_id, "yes")  # log for stats / export
        text = messages.answer_yes(user)
        _update_message(client, body, text, messages.confirm_free_blocks(text, user.get("lang")))

    @app.action("wb_no")
    def on_no(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.get_user(user_id) or {}
        db.record_answer(user_id, "no")
        db.record_event(user_id, "no")  # log for stats / export
        scheduler.add_pending(user_id)
        _update_message(client, body, messages.answer_no(user.get("lang")))

    @app.action("wb_almost")
    def on_almost(ack, body, client):
        ack()
        pm = json.dumps(
            {"channel": body["channel"]["id"], "ts": body["message"]["ts"]}
        )
        client.views_open(
            trigger_id=body["trigger_id"],
            view=messages.almost_done_modal(private_metadata=pm),
        )

    @app.view("wb_almost_modal")
    def on_almost_submit(ack, body, view, client):
        ack()
        user_id = body["user"]["id"]
        user = db.get_user(user_id) or {}
        minutes = int(
            view["state"]["values"]["dur_block"]["dur"]["selected_option"]["value"]
        )
        db.record_answer(user_id, "almost")
        db.record_event(user_id, "almost", detail=str(minutes))  # log + keep the minutes
        db.set_snooze(user_id, (_now_utc() + timedelta(minutes=minutes)).isoformat())

        text = messages.answer_almost(messages.almost_label(minutes), user.get("lang"))
        pm = json.loads(view.get("private_metadata") or "{}")
        if pm.get("channel") and pm.get("ts"):
            try:
                client.chat_update(
                    channel=pm["channel"],
                    ts=pm["ts"],
                    text=text,
                    blocks=messages.confirm_free_blocks(text, user.get("lang")),
                )
            except Exception:
                logger.exception("chat_update failed on almost-done")

    # --- App Home tab -----------------------------------------------------

    @app.event("app_home_opened")
    def on_home_opened(event, client):
        if event.get("tab") != "home":
            return
        db.ensure_user(event["user"], default_interval=DEFAULT_INTERVAL)
        _publish_home(client, event["user"])

    @app.action("wb_checkin_now")
    def on_checkin_now(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        _trigger_checkin_now(client, user_id)
        _publish_home(client, user_id)

    @app.action("wb_pause_today")
    def on_pause_today(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.get_user(user_id)
        if user and user["enabled"] and user["configured"]:
            db.set_paused(user_id, scheduler._end_of_workday_utc(user))
        _publish_home(client, user_id)

    @app.action("wb_free_now")
    def on_free_now(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        user = db.get_user(user_id) or {}
        _trigger_checkin_now(client, user_id)
        # Drop the button on the message that was clicked.
        _update_message(client, body, messages.free_now_ack(user.get("lang")))

    # --- "free" DM (legacy alias for the buttons) -------------------------

    @app.event("message")
    def on_message(event, client):
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = event.get("user")
        text = (event.get("text") or "").strip().lower()
        if not user_id or text != "free":
            return  # silent: only the "free" keyword does anything
        _trigger_checkin_now(client, user_id)
