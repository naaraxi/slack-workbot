"""All user-facing copy and Block Kit layouts for WorkBot.

Every string lives in DEFAULTS below (English). At runtime, strings are read
from per-language JSON files in WORKBOT_STRINGS_DIR (default /data/strings),
one file per language, e.g. en.json, sv.json. Lookup order for a key:

    <user's language>.json  ->  <default language>.json  ->  built-in DEFAULTS

so a missing or blank key in a translation simply falls back. Files are written
as templates on first run and reloaded automatically whenever they change - no
rebuild or restart needed. Keep {placeholders} intact when editing.

Per-user language is auto-detected from the user's Slack locale and stored on
their row; unknown locales use WORKBOT_DEFAULT_LANG.
"""

import json
import logging
import os
import time
from datetime import datetime

from .util import minutes_to_hhmm

logger = logging.getLogger("workbot")

STRINGS_DIR = os.environ.get("WORKBOT_STRINGS_DIR", "/data/strings")
# Legacy single-file location, migrated into <default>.json on first run.
LEGACY_PATH = os.environ.get("WORKBOT_STRINGS_PATH", "/data/strings.json")
DEFAULT_LANG = os.environ.get("WORKBOT_DEFAULT_LANG", "en").lower()
LANGS = {
    l.strip().lower()
    for l in os.environ.get("WORKBOT_LANGS", "en,sv").split(",")
    if l.strip()
} | {DEFAULT_LANG}

DEFAULTS = {
    # /workbot status card + enable/disable
    "status_enabled": (
        "WorkBot is *on* for you. Working hours: *{hours}* ({tz}).\n"
        "I'll check in during those hours to ask what you're working on."
    ),
    "status_disabled": "WorkBot is *off* for you. I won't message you unless you turn it on.",
    "status_fallback_on": "WorkBot is on.",
    "status_fallback_off": "WorkBot is off.",
    "btn_enable": "Enable",
    "btn_disable": "Disable",
    "enabled_confirm": (
        "You're all set - WorkBot is *on*. Working hours: *{hours}* ({tz}).\n"
        "I'll check in during those hours. Tap *Check me in now* on my Home tab "
        "whenever you're between tasks. Run `/workbot` any time to change your "
        "settings or turn it off."
    ),
    "disabled_confirm": (
        "WorkBot is now *off* for you. I won't message you until you turn it "
        "back on with `/workbot`."
    ),
    # Working-hours modal
    "modal_title": "WorkBot setup",
    "modal_submit": "Save",
    "modal_cancel": "Cancel",
    "hours_intro": (
        "Set your working hours. I'll only ever message you between these times, "
        "in your own timezone, and never outside them."
    ),
    "modal_start_label": "Start of working day",
    "modal_end_label": "End of working day",
    "err_end_before_start": "End time must be after start time.",
    "err_no_days": "Pick at least one working day.",
    # Check-in DM + answers
    "checkin": "Do you have a Jira task you're working on right now?",
    "checkin_fallback": "WorkBot check-in: do you have a task?",
    "btn_yes": "Yes",
    "btn_no": "No",
    "btn_almost": "Almost Done",
    "answer_yes": (
        "Great - carry on. I'll check in again in about {interval}. Feel free to "
        "ignore it if you're busy, or tap *I'm free now* the moment you're between "
        "tasks and I'll check in right away."
    ),
    "answer_no": "Thanks. I'll let the channel know you're free for a task.",
    "answer_almost": "Got it - I'll leave you alone for about {label}. Tap *I'm free now* if you finish sooner.",
    "free_ack_off_hours": "Noted - you're outside your working hours, so I'll check in when they next start.",
    "expired": "(This check-in expired - I'll ask again.)",
    "paused": (
        "I'll stop checking in for the rest of today. Tap *I'm free now* below, "
        "or run `/workbot`, whenever you want me to resume."
    ),
    # Almost Done modal
    "almost_title": "Almost done?",
    "almost_submit": "Save",
    "almost_cancel": "Cancel",
    "almost_intro": "Roughly how much longer on this task? I'll hold off until then.",
    "almost_field_label": "Estimated time remaining",
    # Home tab + manual trigger / contextual buttons
    "btn_checkin_now": "Check me in now",
    "btn_free_now": "I'm free now",
    "btn_pause_today": "Pause for today",
    "free_now_ack": "On it - checking in now.",
    "home_blurb": (
        "I check in during your working hours to ask whether you have a task, "
        "and give the team a heads-up when you're free for one. Enable me to start."
    ),
    "home_away": "You're marked away in Slack, so I won't check in until you're back.",
    "home_paused": "Paused for today. Tap *Check me in now* to resume.",
    "home_snoozed": "Snoozed until {time}. Tap *Check me in now* if you finish sooner.",
    "home_offhours": "Outside your working hours. Next check-in {time}.",
    "home_next": "Next check-in {time} ({rel}).",
    "home_active_users": "Total users: {n}",
    "home_stats": "Your check-ins so far: {yes} Yes, {no} No, {almost} Almost Done, {ignored} ignored",
    # Settings modal
    "btn_settings": "Settings",
    "interval_field_label": "Check in every",
    "lang_field_label": "Language",
    "days_field_label": "Working days",
    "settings_saved": (
        "Settings saved. Working hours: *{hours}* ({tz}), checking in every "
        "*{interval}*."
    ),
    # Channel report (needs_task_one for a single person, needs_task_many for 2+).
    # report_prefix is prepended only when WORKBOT_REPORT_MENTION is set.
    "needs_task_one": "{mentions} needs a task!",
    "needs_task_many": "{mentions} need a task!",
    "report_prefix": "{mention} ✋ ",
    # Posted once, when the bot newly joins the report channel.
    "join_message": (
        ":wave: Hi, I'm *WorkBot*. I check in privately with people about whether "
        "they have a task, and post here when someone is free to pick one up. "
        ":raised_hand: It's opt-in: run `/workbot` to join in."
    ),
}

# Check-in interval choices offered in the Settings modal (minutes).
INTERVAL_CHOICES = ["30", "60", "120", "180", "240"]

# Display names for the language dropdown (shown in the language itself).
LANG_NAMES = {"en": "English", "sv": "Svenska"}

# Working-day checkboxes: (label, ISO weekday). Edit labels here to translate.
DAY_CHOICES = [
    ("Monday", "1"),
    ("Tuesday", "2"),
    ("Wednesday", "3"),
    ("Thursday", "4"),
    ("Friday", "5"),
    ("Saturday", "6"),
    ("Sunday", "7"),
]

# Duration choices for the Almost Done modal: (label, minutes). Edit here.
ALMOST_CHOICES = [
    ("15 minutes", "15"),
    ("30 minutes", "30"),
    ("45 minutes", "45"),
    ("1 hour", "60"),
    ("1.5 hours", "90"),
    ("2 hours", "120"),
    ("3 hours", "180"),
    ("4 hours", "240"),
]

# Per-language cache: lang -> {"mtime": float, "data": dict}
_cache = {}


def _path(lang):
    return os.path.join(STRINGS_DIR, f"{lang}.json")


def _load(lang):
    """Return the override dict for a language, reloading on file change."""
    path = _path(lang)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    entry = _cache.get(lang)
    if not entry or entry["mtime"] != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _cache[lang] = {"mtime": mtime, "data": json.load(f)}
            logger.info("Loaded strings for '%s' from %s", lang, path)
        except Exception:
            logger.exception("Bad %s; keeping previous strings", path)
            return (entry or {}).get("data", {})
    return _cache[lang]["data"]


def t(key, lang=None):
    """A string by key: user language, then default language, then built-in."""
    for candidate in ([lang] if lang else []) + [DEFAULT_LANG]:
        value = _load(candidate).get(key)
        if isinstance(value, str) and value != "":
            return value
    return DEFAULTS[key]


def normalize_lang(locale):
    """Map a Slack locale like 'sv-SE' to a supported language, else default."""
    sub = (locale or "").split("-")[0].lower()
    return sub if sub in LANGS else DEFAULT_LANG


def write_templates():
    """Create STRINGS_DIR and a template file per language if missing."""
    try:
        os.makedirs(STRINGS_DIR, exist_ok=True)
    except Exception:
        logger.exception("Could not create %s", STRINGS_DIR)
        return

    help_note = (
        "Edit any value to change what WorkBot says in this language. Keep the "
        "{placeholders} intact. Blank or missing keys fall back to the default "
        "language, then to built-in English. Changes apply within a few seconds."
    )

    # Migrate a pre-existing single-file strings.json into <default>.json.
    default_file = _path(DEFAULT_LANG)
    if not os.path.exists(default_file) and os.path.exists(LEGACY_PATH):
        try:
            with open(LEGACY_PATH, encoding="utf-8") as f:
                legacy = json.load(f)
            legacy["_help"] = help_note
            with open(default_file, "w", encoding="utf-8") as f:
                json.dump(legacy, f, indent=2, ensure_ascii=False)
            logger.info("Migrated %s -> %s", LEGACY_PATH, default_file)
        except Exception:
            logger.exception("Legacy strings migration failed")

    for lang in sorted(LANGS):
        path = _path(lang)
        if os.path.exists(path):
            continue
        try:
            payload = {"_help": help_note}
            payload.update(DEFAULTS)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            logger.info("Wrote strings template %s", path)
        except Exception:
            logger.exception("Could not write %s", path)


# --- plain-string accessors ----------------------------------------------


def answer_no(lang=None):
    return t("answer_no", lang)


def answer_almost(label, lang=None):
    return t("answer_almost", lang).format(label=label)


def disabled_confirm(lang=None):
    return t("disabled_confirm", lang)


def free_ack_off_hours(lang=None):
    return t("free_ack_off_hours", lang)


def err_end_before_start(lang=None):
    return t("err_end_before_start", lang)


def err_no_days(lang=None):
    return t("err_no_days", lang)


def expired(lang=None):
    return t("expired", lang)


def paused(lang=None):
    return t("paused", lang)


def checkin_fallback(lang=None):
    return t("checkin_fallback", lang)


def answer_yes(user):
    lang = user.get("lang")
    return t("answer_yes", lang).format(interval=interval_label(user.get("interval_minutes") or 60))


def enabled_confirm(user):
    lang = user.get("lang")
    return t("enabled_confirm", lang).format(hours=_hours_label(user), tz=user.get("tz") or "unknown")


def settings_saved(user):
    lang = user.get("lang")
    return t("settings_saved", lang).format(
        hours=_hours_label(user),
        tz=user.get("tz") or "unknown",
        interval=interval_label(user.get("interval_minutes") or 60),
    )


def join_message(lang=None):
    return t("join_message", lang)


def needs_task_text(user_ids, lang=None, mention=""):
    mentions = ", ".join(f"<@{u}>" for u in user_ids)
    key = "needs_task_many" if len(user_ids) > 1 else "needs_task_one"
    body = t(key, lang).format(mentions=mentions)
    if mention:
        return t("report_prefix", lang).format(mention=mention) + body
    return body


# --- labels ---------------------------------------------------------------


def _hours_label(user):
    if user.get("configured"):
        return f"{minutes_to_hhmm(user['work_start_min'])}-{minutes_to_hhmm(user['work_end_min'])}"
    return "not set"


def almost_label(minutes):
    for label, value in ALMOST_CHOICES:
        if value == str(minutes):
            return label
    return f"{minutes} minutes"


def interval_label(minutes):
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


# --- block builders -------------------------------------------------------


def status_fallback(user):
    lang = user.get("lang")
    return t("status_fallback_on", lang) if user["enabled"] else t("status_fallback_off", lang)


def _btn(text, action_id, style=None):
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id}
    if style:
        b["style"] = style
    return b


def confirm_blocks(text):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def confirm_free_blocks(text, lang=None):
    """A confirmation message that also carries an 'I'm free now' button."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [_btn(t("btn_free_now", lang), "wb_free_now", "primary")]},
    ]


def paused_blocks(lang=None):
    return confirm_free_blocks(t("paused", lang), lang)


def free_now_ack(lang=None):
    return t("free_now_ack", lang)


def _date_token(ts):
    """Slack token that renders an epoch as the viewer's local time."""
    return f"<!date^{int(ts)}^{{time}}|soon>"


def _date_token_full(ts):
    """Like _date_token but includes the day (for check-ins not today)."""
    return f"<!date^{int(ts)}^{{date_short_pretty}} {{time}}|soon>"


def _rel_time(ts):
    mins = int((ts - time.time()) // 60)
    if mins <= 0:
        return "any moment"
    if mins < 60:
        return f"~{mins} min"
    h, m = divmod(mins, 60)
    return f"~{h}h{m:02d}m" if m else f"~{h}h"


def _iso_to_epoch(iso):
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return None


def _home_state_line(user, st, lang):
    """The dynamic status line for the Home tab, from a status dict."""
    if st.get("away"):
        return t("home_away", lang)
    if st.get("paused_until"):
        return t("home_paused", lang)
    if st.get("snooze_until"):
        ep = _iso_to_epoch(st["snooze_until"])
        return t("home_snoozed", lang).format(time=_date_token(ep) if ep else "later")
    if not st.get("in_hours"):
        ep = st.get("next_ts")
        return t("home_offhours", lang).format(time=_date_token_full(ep) if ep else "later")
    ep = st.get("next_ts")
    if ep:
        return t("home_next", lang).format(time=_date_token(ep), rel=_rel_time(ep))
    return ""


def _active_users_block(st, lang):
    n = st.get("active_users")
    if n is None:
        return None
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": t("home_active_users", lang).format(n=n)}]}


def _stats_block(st, lang):
    # Personal tally, shown only once the user has any history.
    s = st.get("stats")
    if not s or sum(s.values()) == 0:
        return None
    text = t("home_stats", lang).format(yes=s["yes"], no=s["no"], almost=s["almost"], ignored=s["ignored"])
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def home_view(user, st=None):
    """The persistent App Home tab: a status summary, a live state line, the
    active-user count, and the action buttons. `st` is a status dict from the
    handler (away / paused / snoozed / in_hours / next_ts / active_users)."""
    st = st or {}
    lang = user.get("lang")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "WorkBot"}}]

    if not user["enabled"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": t("home_blurb", lang)}})
        for blk in (_active_users_block(st, lang), _stats_block(st, lang)):
            if blk:
                blocks.append(blk)
        blocks.append({"type": "actions", "elements": [
            _btn(t("btn_enable", lang), "wb_enable", "primary"),
            _btn(t("btn_settings", lang), "wb_settings"),
        ]})
        return {"type": "home", "blocks": blocks}

    summary = t("status_enabled", lang).format(hours=_hours_label(user), tz=user.get("tz") or "unknown")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary}})

    state_line = _home_state_line(user, st, lang)
    if state_line:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": state_line}]})

    for blk in (_active_users_block(st, lang), _stats_block(st, lang)):
        if blk:
            blocks.append(blk)

    elements = [
        _btn(t("btn_disable", lang), "wb_disable"),
        _btn(t("btn_settings", lang), "wb_settings"),
        _btn(t("btn_checkin_now", lang), "wb_checkin_now", "primary"),
    ]
    active = st.get("in_hours") and not st.get("away") and not st.get("paused_until") and not st.get("snooze_until")
    if active:
        elements.append(_btn(t("btn_pause_today", lang), "wb_pause_today"))
    blocks.append({"type": "actions", "elements": elements})

    return {"type": "home", "blocks": blocks}


def status_blocks(user):
    lang = user.get("lang")
    if user["enabled"]:
        status = t("status_enabled", lang).format(hours=_hours_label(user), tz=user.get("tz") or "unknown")
    else:
        status = t("status_disabled", lang)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": status}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("btn_enable", lang)},
                    "style": "primary",
                    "action_id": "wb_enable",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("btn_disable", lang)},
                    "action_id": "wb_disable",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("btn_settings", lang)},
                    "action_id": "wb_settings",
                },
            ],
        },
    ]


def _select_option(label, value):
    return {"text": {"type": "plain_text", "text": label}, "value": value}


def _days_input(user, lang):
    cur = {d for d in (user.get("work_days") or "1,2,3,4,5").split(",") if d.strip()}
    opts = [_select_option(label, value) for label, value in DAY_CHOICES]
    element = {"type": "checkboxes", "action_id": "days", "options": opts}
    initial = [o for o in opts if o["value"] in cur]
    if initial:
        element["initial_options"] = initial
    return {
        "type": "input",
        "block_id": "days_block",
        "label": {"type": "plain_text", "text": t("days_field_label", lang)},
        "element": element,
    }


def settings_modal(user, private_metadata="", lang=None):
    """Working hours + check-in interval + language, pre-filled from the user."""
    lang = lang or user.get("lang") or DEFAULT_LANG

    start_time = minutes_to_hhmm(user["work_start_min"]) if user.get("configured") else "09:00"
    end_time = minutes_to_hhmm(user["work_end_min"]) if user.get("configured") else "17:00"

    cur_interval = str(user.get("interval_minutes") or 60)
    if cur_interval not in INTERVAL_CHOICES:
        cur_interval = "60"
    interval_opts = [_select_option(interval_label(int(m)), m) for m in INTERVAL_CHOICES]
    interval_initial = next(o for o in interval_opts if o["value"] == cur_interval)

    lang_opts = [_select_option(LANG_NAMES.get(code, code), code) for code in sorted(LANGS)]
    cur_lang = lang if lang in LANGS else DEFAULT_LANG
    lang_initial = next(o for o in lang_opts if o["value"] == cur_lang)

    return {
        "type": "modal",
        "callback_id": "wb_settings_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": t("modal_title", lang)},
        "submit": {"type": "plain_text", "text": t("modal_submit", lang)},
        "close": {"type": "plain_text", "text": t("modal_cancel", lang)},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": t("hours_intro", lang)}},
            {
                "type": "input",
                "block_id": "start_block",
                "label": {"type": "plain_text", "text": t("modal_start_label", lang)},
                "element": {"type": "timepicker", "action_id": "start", "initial_time": start_time},
            },
            {
                "type": "input",
                "block_id": "end_block",
                "label": {"type": "plain_text", "text": t("modal_end_label", lang)},
                "element": {"type": "timepicker", "action_id": "end", "initial_time": end_time},
            },
            _days_input(user, lang),
            {
                "type": "input",
                "block_id": "interval_block",
                "label": {"type": "plain_text", "text": t("interval_field_label", lang)},
                "element": {
                    "type": "static_select",
                    "action_id": "interval",
                    "options": interval_opts,
                    "initial_option": interval_initial,
                },
            },
            {
                "type": "input",
                "block_id": "lang_block",
                "label": {"type": "plain_text", "text": t("lang_field_label", lang)},
                "element": {
                    "type": "static_select",
                    "action_id": "lang",
                    "options": lang_opts,
                    "initial_option": lang_initial,
                },
            },
        ],
    }


def checkin_blocks(lang=None):
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": t("checkin", lang)}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("btn_yes", lang)},
                    "style": "primary",
                    "action_id": "wb_yes",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("btn_no", lang)},
                    "action_id": "wb_no",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("btn_almost", lang)},
                    "action_id": "wb_almost",
                },
            ],
        },
    ]


def almost_done_modal(private_metadata="", lang=None):
    options = [
        {"text": {"type": "plain_text", "text": label}, "value": value}
        for label, value in ALMOST_CHOICES
    ]
    return {
        "type": "modal",
        "callback_id": "wb_almost_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": t("almost_title", lang)},
        "submit": {"type": "plain_text", "text": t("almost_submit", lang)},
        "close": {"type": "plain_text", "text": t("almost_cancel", lang)},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": t("almost_intro", lang)}},
            {
                "type": "input",
                "block_id": "dur_block",
                "label": {"type": "plain_text", "text": t("almost_field_label", lang)},
                "element": {
                    "type": "static_select",
                    "action_id": "dur",
                    "options": options,
                    "initial_option": options[3],  # 1 hour
                },
            },
        ],
    }
