"""SQLite state for WorkBot.

One row per Slack user. Timestamps are stored as ISO-8601 UTC strings.
Working hours are stored as minutes from local midnight, applied in the
user's own timezone (tz, cached from users.info).

The database is opened per call. WAL mode is enabled once at init so the
Socket Mode handler thread and the scheduler thread can read/write without
locking each other out.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("WORKBOT_DB_PATH", "/data/workbot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id             TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 0,   -- 0/1
    configured          INTEGER NOT NULL DEFAULT 0,   -- 0/1, has set working hours
    tz                  TEXT,                         -- e.g. Europe/Stockholm
    work_start_min      INTEGER,                      -- minutes from local midnight
    work_end_min        INTEGER,
    work_days           TEXT DEFAULT '1,2,3,4,5',     -- ISO weekdays (Mon=1..Sun=7)
    interval_minutes    INTEGER NOT NULL DEFAULT 60,
    snooze_until        TEXT,                         -- ISO-8601 UTC, nullable
    last_asked          TEXT,                         -- ISO-8601 UTC, nullable
    awaiting_reply      INTEGER NOT NULL DEFAULT 0,   -- a check-in is outstanding
    last_msg_channel    TEXT,                         -- DM channel of the open check-in
    last_msg_ts         TEXT,                         -- ts of the open check-in
    consecutive_ignored INTEGER NOT NULL DEFAULT 0,
    paused_until        TEXT,                         -- ISO-8601 UTC, nullable
    last_answer         TEXT,                         -- 'yes' / 'no' / 'almost'
    last_reset_day      TEXT,                         -- local 'YYYY-MM-DD' of last daily reset
    lang                TEXT,                         -- language code, e.g. 'en' / 'sv'
    last_free           TEXT,                         -- ISO-8601 UTC of last accepted 'free' DM
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

# Columns added after the first release; applied to pre-existing databases.
_MIGRATIONS = {
    "awaiting_reply": "INTEGER NOT NULL DEFAULT 0",
    "last_msg_channel": "TEXT",
    "last_msg_ts": "TEXT",
    "consecutive_ignored": "INTEGER NOT NULL DEFAULT 0",
    "paused_until": "TEXT",
    "last_answer": "TEXT",
    "last_reset_day": "TEXT",
    "lang": "TEXT",
    "last_free": "TEXT",
    "work_days": "TEXT DEFAULT '1,2,3,4,5'",
}


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db():
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        conn.executescript(META_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for col, decl in _MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")


def meta_get(key):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def meta_set(key, value):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_user(user_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def ensure_user(user_id, default_interval=60):
    """Insert a blank disabled row if the user is unknown. Returns the row."""
    existing = get_user(user_id)
    if existing:
        return existing
    now = utcnow_iso()
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO users
               (user_id, interval_minutes, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, default_interval, now, now),
        )
    return get_user(user_id)


def _update(user_id, **fields):
    fields["updated_at"] = utcnow_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [user_id]
    with _conn() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE user_id = ?", vals)


# --- settings -------------------------------------------------------------


def set_enabled(user_id, enabled):
    _update(user_id, enabled=1 if enabled else 0)


def set_working_hours(user_id, tz, start_min, end_min):
    _update(
        user_id,
        tz=tz,
        work_start_min=start_min,
        work_end_min=end_min,
        configured=1,
    )


def set_interval(user_id, minutes):
    _update(user_id, interval_minutes=minutes)


def set_lang(user_id, lang):
    _update(user_id, lang=lang)


def set_work_days(user_id, days_csv):
    _update(user_id, work_days=days_csv)


def set_last_free(user_id, when_iso):
    _update(user_id, last_free=when_iso)


# --- snooze (Almost Done) -------------------------------------------------


def set_snooze(user_id, until_iso):
    _update(user_id, snooze_until=until_iso)


def clear_snooze(user_id):
    _update(user_id, snooze_until=None)


# --- check-in lifecycle ---------------------------------------------------


def record_sent(user_id, channel, ts):
    """A check-in was just sent: mark it outstanding and stamp the time."""
    _update(
        user_id,
        awaiting_reply=1,
        last_msg_channel=channel,
        last_msg_ts=ts,
        last_asked=utcnow_iso(),
    )


def record_answer(user_id, answer):
    """The user answered: clear the outstanding flag, ignore counter, pause."""
    _update(
        user_id,
        awaiting_reply=0,
        consecutive_ignored=0,
        last_answer=answer,
        paused_until=None,
    )


def bump_ignored(user_id, count):
    _update(user_id, consecutive_ignored=count)


def set_paused(user_id, until_iso):
    _update(
        user_id,
        paused_until=until_iso,
        awaiting_reply=0,
        last_msg_channel=None,
        last_msg_ts=None,
    )


def resume(user_id):
    """Lift a pause and reset the ignore counter."""
    _update(
        user_id,
        paused_until=None,
        consecutive_ignored=0,
        awaiting_reply=0,
        last_msg_channel=None,
        last_msg_ts=None,
    )


def daily_reset(user_id, day):
    """Clear per-day state at local midnight; keep config (hours, interval)."""
    _update(
        user_id,
        snooze_until=None,
        paused_until=None,
        consecutive_ignored=0,
        awaiting_reply=0,
        last_answer=None,
        last_asked=None,
        last_msg_channel=None,
        last_msg_ts=None,
        last_reset_day=day,
    )


def enabled_users():
    """All rows that are enabled and have finished first-run setup."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE enabled = 1 AND configured = 1"
        ).fetchall()
        return [dict(r) for r in rows]


def active_count():
    """How many users currently have WorkBot enabled."""
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE enabled = 1"
        ).fetchone()["c"]
