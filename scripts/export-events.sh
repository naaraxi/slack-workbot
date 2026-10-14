#!/usr/bin/env bash
#
# Export the full WorkBot event stream (every Yes / No / Almost Done / ignored)
# to a CSV, one row per outcome, oldest first.
#
# Usage: ./export-events.sh [output.csv]
#
set -euo pipefail

# The database sits next to the compose project, so resolve it relative to this
# script rather than the caller's working directory.
DB="$(cd "$(dirname "$0")/.." && pwd)/data/workbot.db"
OUT="${1:-events-$(date +%Y%m%d).csv}"

# Fail early with a clear message rather than a confusing sqlite error.
command -v sqlite3 >/dev/null || { echo "sqlite3 is not installed" >&2; exit 1; }
[ -f "$DB" ] || { echo "database not found: $DB" >&2; exit 1; }

# WAL mode makes this safe to run while the bot is live (readers don't block).
sqlite3 -header -csv "$DB" \
  "SELECT ts, user_id, event, detail FROM events ORDER BY ts;" > "$OUT"

# Report the real row count (sqlite omits the header on an empty result, so
# line-counting the file would be off by one).
N="$(sqlite3 "$DB" "SELECT COUNT(*) FROM events;")"
echo "wrote $N events to $OUT"
