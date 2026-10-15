"""WorkBot entry point: Bolt app over Socket Mode.

Silent by default: the only bot-initiated contact is the scheduler check-in,
which targets only users who have run /workbot, enabled it, and set their
working hours. Everything else is user-initiated.
"""

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import db, handlers, messages, scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("workbot")

REPORT_CHANNEL = os.environ.get("WORKBOT_REPORT_CHANNEL", "")
# Optional user-group handle to ping on reports (resolved to a subteam ID,
# which is cached in the DB). Empty = no prefix.
REPORT_GROUP = os.environ.get("WORKBOT_REPORT_GROUP", "")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
handlers.register(app)


def main():
    db.init_db()
    logger.info("Database ready at %s", db.DB_PATH)
    messages.write_templates()
    scheduler.ensure_in_channel(app.client, REPORT_CHANNEL)
    report_mention = scheduler.resolve_report_mention(app.client, REPORT_GROUP)
    scheduler.start(app.client, REPORT_CHANNEL, report_mention=report_mention)
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("Starting WorkBot (Socket Mode)...")
    handler.start()


if __name__ == "__main__":
    main()
