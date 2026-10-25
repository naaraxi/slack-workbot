# WorkBot

A small Slack bot that checks in with people, privately and on a schedule, to
ask whether they have a task to work on, gives the team a heads-up in a channel
when someone is free, and (optionally) suggests unassigned Jira tasks and reports
back once that person gets one.

It is opt-in and quiet by default: WorkBot never messages anyone until they turn
it on themselves with the `/workbot` command. It is built with
[Bolt for Python](https://slack.dev/bolt-python/) and runs over Socket Mode, so
it needs no public URL.

## What it does

- Each user runs `/workbot` to enable it and set their working hours, check-in
  interval (30m / 1h / 2h / 3h / 4h) and language. Settings also hold their role,
  preferred Jira projects, and which weekdays to check in on.
- During their working hours WorkBot sends a direct message asking if they have
  a task, with three buttons:
  - **Yes** - carry on; it checks again one interval later.
  - **Almost Done** - pick how much longer you need; it stays quiet until then.
  - **No** - WorkBot posts to the configured channel that you are free for a
    task.
- A **No** posts to the channel immediately, one message per person, and can
  optionally mention a user group (for example your project managers) and
  pre-seed acknowledgement reactions.
- An App Home tab shows your status, the next check-in time, and buttons to
  enable/disable, open settings, pause for the day, or check in now.

### Jira features (optional)

These turn on only when the `JIRA_*` variables are set; without them WorkBot
works exactly as above.

- **Task suggestions.** When someone answers **No**, WorkBot replies in the
  report thread with a few unassigned Jira tasks that fit their role. Candidates
  are drawn org-wide, ranked by a local Codex CLI run (bundled in the image, so
  no API billing), with the user's preferred projects as a soft preference.
- **In-thread follow-ups.** Reply in the report thread and address the bot
  (`@WorkBot ...`) to get a fresh set or a quick answer; it replies
  conversationally and only re-suggests when asked. Image and text-less messages
  are ignored.
- **Assignment watch.** Once a reported user actually gets assigned a task,
  WorkBot replies in that same thread ("@user was assigned KEY ... by @assigner")
  and closes it out. Only the user's most recent report is tracked.
- **Home-tab metrics.** When suggestions are on, the Home tab also shows WorkBot's
  Codex token use for the current cycle and the account's rate-limit usage.

### Considerate by design

- Silent by default; only contacts users who opted in.
- Respects Slack status: it skips anyone marked out of office (configurable set
  of status emojis).
- Applies each user's own timezone and working hours.
- Stops asking after a few ignored check-ins in a row, until the next day.
- Never reports anyone to the channel for simply not replying - only an
  explicit **No** is shared.

## Requirements

- A Slack app (see setup below) with Socket Mode enabled.
- Docker and Docker Compose.
- For the Jira features: a Jira Cloud site and an API token. The image also
  bundles the Codex CLI and uses an organization ChatGPT/Codex subscription for
  the suggestion ranking (see the Dockerfile and `.codex-home/`).

## Slack app setup

Create an app at <https://api.slack.com/apps> (from scratch is fine) and:

1. **Socket Mode**: enable it, and generate an app-level token with the
   `connections:write` scope (this is your `SLACK_APP_TOKEN`, `xapp-...`).
2. **OAuth & Permissions - Bot Token Scopes**:
   - Core: `commands`, `chat:write`, `chat:write.public`, `im:write`,
     `im:history`, `im:read`, `users:read`, `channels:read`, `channels:join`
     (resolve and join the report channel), `reactions:write` and
     `reactions:read` (acknowledgement reactions).
   - For task suggestions and in-thread follow-ups: `channels:history` (read
     replies in the report thread).
   - Optional: `usergroups:read` to mention a user group on reports;
     `users:read.email` for the assignment watch (to map Slack users to Jira
     accounts by email).
3. **Event Subscriptions - bot events**: `message.im` and `app_home_opened`;
   add `message.channels` to enable the in-thread follow-ups.
4. **Slash Commands**: create `/workbot` (no request URL needed under Socket
   Mode).
5. **App Home**: turn on the **Home Tab** and the Messages tab.
6. Install the app to your workspace and copy the **Bot User OAuth Token**
   (`xoxb-...`, your `SLACK_BOT_TOKEN`).

## Running

```bash
cp .env.example .env
# edit .env: set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and WORKBOT_REPORT_CHANNEL
docker compose up -d --build
```

### Dependencies

- Python packages are installed only from `requirements.txt`, which pins exact
  versions with hashes (`pip --require-hashes`). The direct dependencies are
  listed in `requirements.in`; the command to update the lock is at the top of
  that file.
- Node (for the Codex CLI) comes from the official `node:22` image in a build
  stage. Only `node` and Codex are copied into the final image. The Codex version
  is pinned in the `Dockerfile`.
- The base image is `python:3.12-slim`. Rebuild with `docker compose build --pull`
  to take Debian and Python fixes.

State (per-user settings, report threads, token counters) is kept in a SQLite
database under `data/`, which is a mounted volume. All user-facing text is
generated into `data/strings/` as one JSON file per language on first run; edit
those to change wording or translate, and the bot picks up changes within a few
seconds, no restart needed.

### Enabling the Jira features

1. Set `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_TOKEN` (an API token from
   id.atlassian.com). This alone adds the per-user project picker in Settings.
2. Set `WORKBOT_SUGGEST_ENABLED=1` to suggest tasks on a **No**. Tune which
   statuses count as candidates with `WORKBOT_CANDIDATE_STATUSES` (allowlist) or
   `WORKBOT_CANDIDATE_EXCLUDE_STATUSES` (denylist); the default is any "To Do"
   status category.
3. Set `WORKBOT_ASSIGN_WATCH_ENABLED=1` for the assignment watch. It needs the
   `users:read.email` scope and the per-user Jira account IDs, cached once with:

   ```bash
   docker compose exec workbot python -m workbot.map_jira
   ```

   Re-run it after someone joins (or with `--refresh` to re-resolve everyone).

## Configuration

All configuration is through environment variables; see `.env.example` for the
full list and descriptions. The main ones:

| Variable | Purpose |
| --- | --- |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-Level Token for Socket Mode (`xapp-...`) |
| `WORKBOT_REPORT_CHANNEL` | Channel for "needs a task" reports (ID or `#name`) |
| `WORKBOT_DEFAULT_INTERVAL_MINUTES` | Default check-in interval for new users |
| `WORKBOT_LANGS` / `WORKBOT_DEFAULT_LANG` | Languages offered, and the fallback |
| `WORKBOT_REPORT_GROUP` | Optional user-group handle to ping on reports |
| `WORKBOT_PRUNE_AFTER_HOURS` | Delete the bot's own DMs older than this (0 to disable) |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_TOKEN` | Jira Cloud site and API token |
| `WORKBOT_SUGGEST_ENABLED` | Suggest Jira tasks on a No (default off) |
| `WORKBOT_SUGGEST_REPEAT_DAYS` | Skip tasks offered to the same user in the last N days (default 3) |
| `WORKBOT_CANDIDATE_STATUSES` / `_EXCLUDE_STATUSES` | Which statuses are eligible |
| `WORKBOT_ASSIGN_WATCH_ENABLED` | Announce in-thread when a user is assigned a task |

## License

See `LICENSE`.
