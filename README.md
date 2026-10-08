# WorkBot

A small Slack bot that checks in with people, privately and on a schedule, to
ask whether they have a task to work on, and gives the team a heads-up in a
channel when someone is free to pick one up.

It is opt-in and quiet by default: WorkBot never messages anyone until they
turn it on themselves with the `/workbot` command. It is built with
[Bolt for Python](https://slack.dev/bolt-python/) and runs over Socket Mode, so
it needs no public URL.

## What it does

- Each user runs `/workbot` to enable it and set their working hours, check-in
  interval (30m / 1h / 2h / 3h / 4h) and language.
- During their working hours WorkBot sends a direct message asking if they have
  a task, with three buttons:
  - **Yes** - carry on; it checks again one interval later.
  - **Almost Done** - pick how much longer you need; it stays quiet until then.
  - **No** - WorkBot posts to the configured channel that you are free for a
    task.
- Reports are batched into one channel message over a short window, and can
  optionally mention a user group (for example your project managers).
- An App Home tab shows your status, the next check-in time, and buttons to
  enable/disable, open settings, pause for the day, or check in now.

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

## Slack app setup

Create an app at <https://api.slack.com/apps> (from scratch is fine) and:

1. **Socket Mode**: enable it, and generate an app-level token with the
   `connections:write` scope (this is your `SLACK_APP_TOKEN`, `xapp-...`).
2. **OAuth & Permissions - Bot Token Scopes**: `commands`, `chat:write`,
   `chat:write.public`, `im:write`, `im:history`, `users:read`. Add
   `usergroups:read` only if you want the report to mention a user group.
3. **Event Subscriptions - bot events**: `message.im` and `app_home_opened`.
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

State (per-user settings) is kept in a SQLite database under `data/`, which is
a mounted volume. All user-facing text is generated into `data/strings/` as one
JSON file per language on first run; edit those to change wording or translate,
and the bot picks up changes within a few seconds, no restart needed.

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

## License

See `LICENSE`.
