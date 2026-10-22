"""Jira task-suggestion step.

When a user answers No, this replies in the notification's thread with a few
unassigned tasks that fit the user's role.

Flow:
  - An hourly job caches the org-wide unassigned pool (jira.fetch_candidates),
    so handling a No needs no live Jira call.
  - On a No, we filter that cache to the user's projects, let Codex rank the 3
    best by role, and post them as a threaded reply (priority emoji + labeled
    link, no preview).

Everything degrades gracefully: any failure (Jira, Codex, Slack) just means no
suggestion reply - the notification itself still stands.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from . import db, jira, messages

logger = logging.getLogger("workbot")

# Suggestions are on only when explicitly enabled AND Jira is configured.
# Default off: a fresh install won't call Codex/Jira until you opt in.
ENABLED = os.environ.get("WORKBOT_SUGGEST_ENABLED", "0") not in ("0", "", "false", "no")
CACHE_MAX = int(os.environ.get("WORKBOT_SUGGEST_CACHE_MAX", "2000"))    # org-wide fetch cap (cached hourly, so generous)
CODEX_INPUT_MAX = int(os.environ.get("WORKBOT_SUGGEST_INPUT_MAX", "200"))  # passed to Codex
CODEX_TIMEOUT = int(os.environ.get("WORKBOT_SUGGEST_TIMEOUT", "90"))
# How much of the thread we feed back to Codex on a follow-up (last N messages,
# trimmed to a char budget) - keeps each conversational reply bounded in tokens.
# Replies themselves are uncapped: the terse "idk, have a task" style naturally
# discourages long chats, and the bot never replies to itself (bot_id filtered).
FOLLOWUP_CONTEXT_MSGS = int(os.environ.get("WORKBOT_SUGGEST_CONTEXT_MSGS", "12"))
FOLLOWUP_CONTEXT_CHARS = int(os.environ.get("WORKBOT_SUGGEST_CONTEXT_CHARS", "2000"))
# Variety across Nos: tasks already offered to a user in the last N days are left
# out of their next pick (0 turns this off). Without it the same input gives the
# same top 3 every time. Priorities listed in REPEAT_KEEP stay eligible anyway, so
# an urgent ticket keeps surfacing until someone takes it.
REPEAT_DAYS = int(os.environ.get("WORKBOT_SUGGEST_REPEAT_DAYS", "3"))
REPEAT_KEEP = {p.strip() for p in os.environ.get("WORKBOT_SUGGEST_REPEAT_KEEP", "Highest,High").split(",") if p.strip()}

# Codex writes session rollouts (which carry the rate-limit snapshot behind
# `/status`) under here; we read the newest one to surface account usage.
CODEX_HOME = os.environ.get("CODEX_HOME", "/app/.codex-home")

PRIORITY_EMOJI = {"Highest": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢", "Lowest": "🔵"}


def enabled():
    return ENABLED and jira.configured()


def refresh_candidates():
    """Hourly: cache the org-wide unassigned pool so No handling needs no Jira call."""
    if not enabled():
        return
    cands = jira.fetch_candidates(CACHE_MAX)
    if cands:
        db.meta_set("jira_candidates", json.dumps(cands))
        logger.info("Cached %d Jira candidate tasks", len(cands))


def _recently_offered(user_id):
    """Keys offered to this user within REPEAT_DAYS (any thread), or an empty set."""
    if REPEAT_DAYS <= 0:
        return set()
    since = (datetime.now(timezone.utc) - timedelta(days=REPEAT_DAYS)).isoformat()
    return db.recent_suggested(user_id, since)


def _candidates_for(user, avoid=()):
    """The org-wide cached pool, capped. The user's selected projects are a soft
    preference (handled in the prompt), not a filter - but we move their project
    tasks to the front so they always survive the input cap. Keys in `avoid`
    (recent offers) are dropped unless their priority is in REPEAT_KEEP."""
    cands = json.loads(db.meta_get("jira_candidates") or "[]")
    if avoid:
        fresh = [c for c in cands if c["key"] not in avoid or c.get("priority") in REPEAT_KEEP]
        # Only skip repeats when enough fresh tasks are left to fill a pick.
        if len(fresh) >= 3:
            cands = fresh
    projects = {k for k in (user.get("jira_projects") or "").split(",") if k.strip()}
    if projects:
        preferred = [c for c in cands if c.get("project") in projects]
        others = [c for c in cands if c.get("project") not in projects]
        cands = preferred + others
    return cands[:CODEX_INPUT_MAX]


def _listing(candidates):
    """The candidate pool as one line per issue, for the model's <stdin>."""
    return "\n".join(
        f"{c['key']} [{c['priority']}, {c['project']}] {c['summary']}" for c in candidates
    )


def _pref_clause(projects):
    """A soft project-preference sentence, or '' when the user picked no projects."""
    if not projects:
        return ""
    return (
        f" They usually work on projects {', '.join(projects)}, so prefer tasks in those - "
        "but a strong role fit in another project is fine."
    )


def _call_tokens(stdout):
    """Total tokens for a run, from the `--json` turn.completed event (input +
    output), or 0 if it can't be read."""
    total = 0
    for line in (stdout or "").splitlines():
        if '"turn.completed"' not in line:
            continue
        try:
            u = json.loads(line).get("usage") or {}
            total = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        except Exception:
            pass
    return total


def _latest_rate_limits():
    """The most recent rate-limit snapshot Codex recorded (the data behind
    `/status`), read from the newest session rollout, or None. Codex only writes
    it to the rollout - there's no `codex usage` subcommand - so this is the way."""
    sessions = os.path.join(CODEX_HOME, "sessions")
    newest, newest_mtime = None, -1.0
    for root, _dirs, files in os.walk(sessions):
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                p = os.path.join(root, name)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m > newest_mtime:
                    newest, newest_mtime = p, m
    if not newest:
        return None
    rl = None
    try:
        with open(newest) as f:
            for line in f:
                if '"token_count"' not in line:
                    continue
                payload = (json.loads(line).get("payload") or {})
                if payload.get("rate_limits"):
                    rl = payload["rate_limits"]  # keep the last one in the file
    except Exception:
        logger.exception("Could not read rate limits from %s", newest)
    return rl


def _record_usage(stdout, meter):
    """After a Codex run: refresh the cached rate-limit snapshot, and (for metered
    runs only) add this run's tokens to the current cycle's counter. The cycle is
    keyed to the weekly rate-limit window, so it rolls over when that resets."""
    rl = _latest_rate_limits()
    resets_at = None
    if rl:
        db.meta_set("codex_rate_limits", json.dumps(rl))
        resets_at = (rl.get("primary") or {}).get("resets_at")
    if meter:
        db.add_codex_tokens(_call_tokens(stdout), resets_at)


def _codex_run(prompt, listing, meter=True):
    """Run one Codex turn with the candidate list on stdin; return its answer text
    (or '' on any failure). OPENAI_API_KEY is stripped from the child env so Codex
    authenticates with the org subscription, not a personal API key. Every run
    refreshes the usage snapshot; metered runs also count toward the token cycle
    (the hourly usage poll passes meter=False so polling doesn't inflate it)."""
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    out_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            out_path = f.name
        proc = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--json", "-o", out_path, prompt],
            input=listing, cwd="/tmp", env=env, text=True,
            capture_output=True, timeout=CODEX_TIMEOUT,
        )
        try:
            _record_usage(proc.stdout, meter)
        except Exception:
            logger.exception("Could not record Codex usage")
        with open(out_path) as f:
            return f.read()
    except Exception:
        logger.exception("Codex run failed")
        return ""
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def refresh_usage():
    """Hourly: a tiny Codex query whose only purpose is to refresh the account's
    rate-limit snapshot (so the Home tab's usage stays current even when the bot
    makes no suggestions). Not metered - this poll shouldn't inflate the token
    cycle. No-ops unless suggestions are enabled."""
    if not enabled():
        return
    _codex_run("Reply with just: ok", "", meter=False)
    logger.info("Refreshed Codex usage snapshot")


def usage_summary():
    """Display-ready Codex metrics for the Home tab, or None when nothing is known
    yet: {tokens, percent, window_minutes, resets_at, plan, secondary?}."""
    tokens = db.meta_get("codex_tokens_cycle")
    raw = db.meta_get("codex_rate_limits")
    if tokens is None and not raw:
        return None
    out = {"tokens": int(tokens) if tokens is not None else None}
    if raw:
        try:
            rl = json.loads(raw)
            primary = rl.get("primary") or {}
            out.update({
                "percent": primary.get("used_percent"),
                "window_minutes": primary.get("window_minutes"),
                "resets_at": primary.get("resets_at"),
                "plan": rl.get("plan_type"),
            })
            secondary = rl.get("secondary")
            if secondary:
                out["secondary"] = {
                    "percent": secondary.get("used_percent"),
                    "window_minutes": secondary.get("window_minutes"),
                    "resets_at": secondary.get("resets_at"),
                }
        except Exception:
            logger.exception("Could not parse cached rate limits")
    return out


def _codex_pick(candidates, role, projects):
    """Initial suggestion: the 3 best-fit issue keys for the role; [] on failure.
    Projects are a soft preference, not a hard filter."""
    prompt = (
        f"A {role} team member needs a task.{_pref_clause(projects)} The <stdin> block lists "
        "candidate Jira issues, one per line as 'KEY [priority, project] summary'. Choose the 3 "
        f"that best fit a {role}, balancing fit and priority. Pick Low or Lowest priority tasks "
        "only as a last resort, when nothing of higher priority fits the role. Output ONLY the 3 "
        "issue keys, comma-separated, nothing else. Use only the list; do not use any tools."
    )
    return re.findall(r"[A-Z][A-Z0-9]+-\d+", _codex_run(prompt, _listing(candidates)))[:3]


def _codex_followup(candidates, role, projects, convo, exclude):
    """Follow-up: the member replied in the thread after an earlier suggestion.
    This uses a conversational prompt (distinct from the one-shot initial pick):
    the model reads the thread and decides whether tasks are even warranted.
    Returns (reply_text, keys) - a short natural message plus up to 3 new task
    keys, or ('', []) on failure so the caller can stay silent."""
    exclude_part = (
        f" You have already suggested {', '.join(exclude)}; never offer those again."
        if exclude else ""
    )
    prompt = (
        f"You are WorkBot, a warm, upbeat teammate with a light sense of humour, helping a "
        f"{role or 'team'} teammate pick an unassigned Jira task in a Slack thread. Talk like a "
        "friendly colleague, not a form: natural, relaxed, a little playful. Never sound stiff or "
        f"robotic.{_pref_clause(projects)}{exclude_part} The <stdin> block lists candidate issues, "
        "one per line as 'KEY [priority, project] summary'. Here is the thread so far, oldest "
        f"first:\n\"\"\"\n{convo}\n\"\"\"\n"
        "Respond to their LAST message:\n"
        f"- If they want different or more tasks, choose 3 fresh ones that fit a {role or 'team'} "
        "member (balance fit and priority; Low/Lowest only as a last resort) and add a cheerful "
        "one-line intro.\n"
        "- If they accept or pick a task, cheer them on and suggest NO tasks.\n"
        "- If the message is off-topic or silly, do NOT earnestly answer it: give a short, "
        "good-natured quip that plays along for a beat, then breezily steer back to tasks, and "
        "suggest NO tasks unless they asked for them.\n"
        "Reply with ONE line of JSON and nothing else: "
        '{"reply": "<your message to them, max 30 words>", "keys": ["KEY1","KEY2","KEY3"]}. '
        'Use "keys": [] when you are not suggesting tasks. Use only keys from the list. Do not '
        "use any tools."
    )
    answer = _codex_run(prompt, _listing(candidates))
    if not answer:
        return "", []
    # Pull the JSON object out of the answer, tolerating any stray text around it.
    data = {}
    try:
        m = re.search(r"\{.*\}", answer, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
    except Exception:
        logger.exception("Could not parse follow-up JSON: %r", answer[:200])
    reply = (data.get("reply") or "").strip()
    keys = [k for k in (data.get("keys") or []) if isinstance(k, str)][:3]
    # Last-ditch: JSON unusable but the model named some keys in prose - use them.
    if not reply and not keys:
        keys = re.findall(r"[A-Z][A-Z0-9]+-\d+", answer)[:3]
    return reply, keys


def _pick(user, candidates):
    """Initial pick: up to 3 (key, priority) - Codex by role (+ project
    preference) when possible, else top-3 by priority."""
    by_key = {c["key"]: c for c in candidates}
    role = (user.get("role") or "").replace(",", "/")  # multi-role -> "Backend/DevOps"
    projects = sorted({k for k in (user.get("jira_projects") or "").split(",") if k.strip()})
    if ENABLED and role and len(candidates) > 3:
        keys = _codex_pick(candidates, role, projects)
        picks = [(k, by_key[k]["priority"]) for k in keys if k in by_key][:3]
        if picks:
            return picks
    # Fallback: candidates are already priority-ordered.
    return [(c["key"], c["priority"]) for c in candidates[:3]]


def _pick_lines(picks):
    """Render picks as emoji + labeled Jira links, one per line."""
    base = jira.BASE_URL
    return "\n".join(f"{PRIORITY_EMOJI.get(p, '⚪')} <{base}/browse/{k}|{k}>" for k, p in picks)


def _post(client, channel, thread_ts, text):
    """Post a threaded reply with link previews off (Jira links stay as labels)."""
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text,
                            unfurl_links=False, unfurl_media=False)


def _post_picks(client, channel, thread_ts, picks):
    """Post a set of picks as a threaded reply (emoji + labeled link, no preview)."""
    _post(client, channel, thread_ts, messages.suggest_header() + "\n" + _pick_lines(picks))


def _reply(client, user_id, channel, thread_ts):
    user = db.get_user(user_id)
    if not user:
        return
    candidates = _candidates_for(user, _recently_offered(user_id))
    if not candidates:
        return  # nothing in scope to suggest
    picks = _pick(user, candidates)
    if not picks:
        return
    try:
        _post_picks(client, channel, thread_ts, picks)
    except Exception:
        logger.exception("Could not post suggestions for %s", user_id)
        return
    # Remember the thread so the user can reply in it for a re-suggest.
    db.record_report_thread(channel, thread_ts, user_id, ",".join(k for k, _ in picks))


def spawn(client, user_id, channel, thread_ts):
    """Run the suggestion + reply in the background (Codex takes a few seconds),
    so the No handler returns immediately."""
    if not enabled():
        return
    threading.Thread(target=_reply, args=(client, user_id, channel, thread_ts), daemon=True).start()


def _thread_transcript(client, channel, thread_ts):
    """The thread as 'name: text' lines (last FOLLOWUP_CONTEXT_MSGS, trimmed to
    FOLLOWUP_CONTEXT_CHARS), so Codex sees the back-and-forth without ballooning
    the prompt. The bot's own lines are labelled 'WorkBot'."""
    resp = client.conversations_replies(
        channel=channel, ts=thread_ts, limit=max(FOLLOWUP_CONTEXT_MSGS, 20)
    )
    msgs = resp.get("messages", [])[-FOLLOWUP_CONTEXT_MSGS:]
    lines = []
    for m in msgs:
        who = "WorkBot" if m.get("bot_id") else (m.get("user") or "user")
        text = (m.get("text") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"{who}: {text}")
    convo = "\n".join(lines)
    return convo[-FOLLOWUP_CONTEXT_CHARS:]


def _has_image(event):
    """True if the message carries an image/gif attachment - we don't engage with
    those (the model only reads text, so there'd be nothing to go on)."""
    return any(
        (f.get("mimetype") or "").startswith("image/") for f in (event.get("files") or [])
    )


def _followup(client, event):
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    # Nothing to respond to: an image/gif post, or a message with no text.
    if _has_image(event) or not (event.get("text") or "").strip():
        return
    # Anyone in the thread may get a reply (no caps). Suggestions stay anchored to
    # the user the thread is about, so role/projects come from that user, not the
    # author. The bot never replies to itself (bot_id filtered in the handler).
    row = db.get_report_thread(channel, thread_ts)
    if not row:
        return  # not a thread we suggested in
    user = db.get_user(row["user_id"])
    if not user:
        return
    candidates = _candidates_for(user, _recently_offered(row["user_id"]))
    if not candidates:
        return
    try:
        convo = _thread_transcript(client, channel, thread_ts)
    except Exception:
        logger.exception("Could not read thread %s", thread_ts)
        return
    role = (user.get("role") or "").replace(",", "/")
    projects = sorted({k for k in (user.get("jira_projects") or "").split(",") if k.strip()})
    already = [k for k in (row.get("suggested") or "").split(",") if k]
    # Conversational turn: a short reply, plus fresh task keys only if warranted.
    pool = [c for c in candidates if c["key"] not in set(already)]
    reply, keys = _codex_followup(pool, role, projects, convo, already)
    if not reply and not keys:
        return  # Codex failed / nothing to say - stay silent
    by_key = {c["key"]: c for c in candidates}
    picks = [(k, by_key[k]["priority"]) for k in keys if k in by_key][:3]
    text = reply or messages.suggest_header()
    if picks:
        text = f"{text}\n{_pick_lines(picks)}".strip()
    try:
        _post(client, channel, thread_ts, text)
    except Exception:
        logger.exception("Could not post follow-up for %s", row["user_id"])
        return
    if picks:
        db.add_thread_suggested(channel, thread_ts, [k for k, _ in picks])


def spawn_followup(client, event):
    """Handle a reply in a report thread in the background (Codex takes a few
    seconds). No-ops unless suggestions are enabled; all gating lives in
    _followup, which ignores anything that isn't a tracked report thread."""
    if not enabled():
        return
    threading.Thread(target=_followup, args=(client, event), daemon=True).start()
