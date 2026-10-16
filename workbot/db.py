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
    jira_projects       TEXT,                         -- CSV of Jira project keys for suggestions
    role                TEXT,                         -- CSV of roles (Backend, Frontend, ...)
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
    "jira_projects": "TEXT",
    "role": "TEXT",
    "jira_account_id": "TEXT",  # resolved from the Slack email, cached for assignment detection
    "jira_email": "TEXT",       # the email it was resolved from (for reference / re-verification)
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

# One row per check-in outcome, kept forever as the history to graph from.
EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event   TEXT NOT NULL,   -- 'yes' / 'no' / 'almost' / 'ignored'
    detail  TEXT,            -- e.g. the Almost Done minutes; else NULL
    ts      TEXT NOT NULL    -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

# One row per report thread we posted suggestions in, so a later reply in that
# thread can be matched to the user it is about and answered with a re-suggest.
# `suggested` accumulates the keys already offered (so we don't repeat them) and
# `replies` counts the bot's own follow-ups, to cap the back-and-forth.
REPORT_THREADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_threads (
    channel    TEXT NOT NULL,
    thread_ts  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    suggested  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
"""


def init_db():
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        conn.executescript(META_SCHEMA)
        conn.executescript(EVENTS_SCHEMA)
        conn.executescript(REPORT_THREADS_SCHEMA)
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


def add_codex_tokens(n, resets_at):
    """Add `n` tokens to the current cycle's running total (meta key
    'codex_tokens_cycle'). The cycle is keyed to the weekly rate-limit window: when
    `resets_at` differs from the stored one, a new window has begun and the count
    restarts. Done in one transaction so concurrent runs don't lose increments."""
    if not n:
        return
    key_at = str(resets_at) if resets_at is not None else ""
    with _conn() as conn:
        def _get(k):
            r = conn.execute("SELECT value FROM meta WHERE key = ?", (k,)).fetchone()
            return r["value"] if r else None

        def _set(k, v):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )

        # A changed reset time means a fresh window - start the count over.
        if key_at and key_at != (_get("codex_cycle_resets_at") or ""):
            _set("codex_cycle_resets_at", key_at)
            _set("codex_tokens_cycle", n)
        else:
            _set("codex_tokens_cycle", int(_get("codex_tokens_cycle") or 0) + n)


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


def set_jira_projects(user_id, keys_csv):
    _update(user_id, jira_projects=keys_csv)


def set_role(user_id, roles_csv):
    _update(user_id, role=roles_csv)


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


def claim_checkin(user_id, msg_ts):
    """Atomically mark the current check-in as answered, but only if `msg_ts` is
    that user's outstanding check-in and they are still enabled. Returns True at
    most once per check-in, so concurrent or repeated clicks can't double-record.
    """
    if not msg_ts:
        return False
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE users SET awaiting_reply = 0, updated_at = ? "
            "WHERE user_id = ? AND enabled = 1 AND awaiting_reply = 1 AND last_msg_ts = ?",
            (utcnow_iso(), user_id, msg_ts),
        )
        return cur.rowcount == 1


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


# --- report threads (for in-thread follow-ups) ---------------------------

def record_report_thread(channel, thread_ts, user_id, suggested_csv):
    """Remember that we suggested tasks in this thread, so a later reply can be
    matched to the user it's about. Replaces any prior row for the thread."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO report_threads "
            "(channel, thread_ts, user_id, suggested, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, thread_ts, user_id, suggested_csv or "", utcnow_iso()),
        )


def get_report_thread(channel, thread_ts):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_threads WHERE channel = ? AND thread_ts = ?",
            (channel, thread_ts),
        ).fetchone()
        return dict(row) if row else None


def add_thread_suggested(channel, thread_ts, keys):
    """Append newly offered keys to the thread's accumulated 'already suggested'
    set, so later follow-ups don't repeat them."""
    if not keys:
        return
    with _conn() as conn:
        row = conn.execute(
            "SELECT suggested FROM report_threads WHERE channel = ? AND thread_ts = ?",
            (channel, thread_ts),
        ).fetchone()
        if not row:
            return
        have = [k for k in (row["suggested"] or "").split(",") if k]
        merged = have + [k for k in keys if k not in have]
        conn.execute(
            "UPDATE report_threads SET suggested = ? WHERE channel = ? AND thread_ts = ?",
            (",".join(merged), channel, thread_ts),
        )


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


def all_users():
    """Every known user row, regardless of enabled/configured state."""
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


def set_jira_identity(user_id, account_id, email):
    """Cache a user's resolved Jira accountId (+ the email it came from)."""
    _update(user_id, jira_account_id=account_id, jira_email=email)


def active_count():
    """How many users currently have WorkBot enabled."""
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE enabled = 1"
        ).fetchone()["c"]


def record_event(user_id, event, detail=None):
    """Append one check-in outcome to the history log."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO events (user_id, event, detail, ts) VALUES (?, ?, ?, ?)",
            (user_id, event, detail, utcnow_iso()),
        )


def event_counts(user_id):
    """All-time per-outcome counts for one user, zero-filled."""
    counts = {"yes": 0, "no": 0, "almost": 0, "ignored": 0}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT event, COUNT(*) AS c FROM events WHERE user_id = ? GROUP BY event",
            (user_id,),
        ).fetchall()
    for r in rows:
        counts[r["event"]] = r["c"]
    return counts
